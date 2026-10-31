"""Conversation/session management endpoints."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..db import database as db
from ..services import chart_store
from ..services.authz import require_session_owner
from ..services.user import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class CreateSessionRequest(BaseModel):
    title: Optional[str] = "New session"


class UpdateSessionRequest(BaseModel):
    title: str


#: Longest title we store. The sidebar truncates well past this, so the cap is about
#: what lands in the database, not about layout. Mirrored in the client's `maxLength`
#: so the input cannot promise more than will be kept — the server is authoritative
#: and returns what it actually stored.
MAX_TITLE_LEN = 120


def _clean_title(raw: str) -> str:
    """One trimmed line, never empty. A title is displayed, not parsed."""
    title = " ".join((raw or "").split())  # collapses newlines and runs of spaces
    if not title:
        raise HTTPException(status_code=400, detail="A session title cannot be empty")
    return title[:MAX_TITLE_LEN]


@router.get("/sessions")
async def list_sessions(request: Request):
    if not db.is_configured():
        return []
    try:
        return await asyncio.to_thread(db.list_sessions, 100)
    except Exception:
        logger.exception("list_sessions failed")
        return []


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str):
    if not db.is_configured():
        raise HTTPException(status_code=503, detail="Persistence not configured")
    try:
        meta = await asyncio.to_thread(db.get_session_meta, session_id) or {}
        messages = await asyncio.to_thread(db.get_session_messages, session_id)
        owner = meta.get("owner_email")
        # Reading is open to everyone; the server decides whether this viewer may
        # write, so the client never has to infer it.
        return {
            "session_id": session_id,
            "title": meta.get("title"),
            "owner_email": owner,
            "owner_name": meta.get("owner_name"),
            "read_only": bool(owner and owner != get_current_user(request)),
            "messages": messages,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_session failed")
        raise HTTPException(status_code=500, detail="Failed to load session")


@router.post("/sessions")
async def create_session(request: Request, body: CreateSessionRequest):
    user_email = get_current_user(request)
    session_id = str(uuid.uuid4())
    if not db.is_configured():
        return {"id": session_id, "title": body.title}
    try:
        return await asyncio.to_thread(
            db.create_session,
            session_id=session_id,
            owner_email=user_email,
            title=body.title,
        )
    except Exception:
        logger.exception("create_session failed")
        raise HTTPException(status_code=500, detail="Failed to create session")


@router.patch("/sessions/{session_id}")
async def update_session(request: Request, session_id: str, body: UpdateSessionRequest):
    await require_session_owner(request, session_id)
    title = _clean_title(body.title)
    if not db.is_configured():
        return {"success": True, "title": title}
    try:
        # touch=False: renaming is not activity. See db.update_session.
        ok = await asyncio.to_thread(db.update_session, session_id, touch=False, title=title)
        if not ok:
            raise HTTPException(status_code=404, detail="Session not found")
        # Return the stored title, not the submitted one — cleaning and the length cap
        # happen here, so this is what the sidebar should show.
        return {"success": True, "title": title}
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_session failed")
        raise HTTPException(status_code=500, detail="Failed to update session")


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    await require_session_owner(request, session_id)
    if not db.is_configured():
        return {"success": True}
    # Chart specs are keyed by filename and carry no session id, so the only link back to
    # this session is the fences inside its own answers — which means the cleanup has to
    # run *before* those messages are deleted. Best-effort: an orphaned spec is harmless,
    # failing here would block the delete the user actually asked for.
    await chart_store.delete_for_session(session_id)
    try:
        ok = await asyncio.to_thread(db.delete_session, session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to delete session")
