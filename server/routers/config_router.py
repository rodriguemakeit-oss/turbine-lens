"""Configuration and user identity endpoints."""

import asyncio

from fastapi import APIRouter, Request

from pydantic import BaseModel

from ..config import GENIE_SPACES, MODEL, get_genie_mode, get_workspace_host
from ..db import database as db
from ..db.database import is_configured as db_configured
from ..db.database import is_healthy as db_healthy
from ..services.user import get_current_user, get_current_user_name

router = APIRouter()


@router.get("/config")
async def get_config():
    return {
        "model": MODEL,
        "workspace_host": get_workspace_host(),
        "genie_spaces": list(GENIE_SPACES.keys()),
        # enabled = actually usable; configured = env vars present. When these
        # disagree, persistence fell back to the volatile in-memory store and the
        # UI needs to say so instead of pretending sessions are being saved.
        "persistence_enabled": db_healthy(),
        "persistence_configured": db_configured(),
        "genie_mode": get_genie_mode(),  # global default
    }


class GenieModeRequest(BaseModel):
    mode: str  # "agent" or "classic"
    session_id: str | None = None


@router.get("/genie_mode")
async def get_genie_mode_endpoint(session_id: str | None = None):
    """Get genie mode for a session.

    Policy:
    - New chat (no session_id): default to agent.
    - Existing chat (session_id set, no DB mode): default to classic.
    """
    if session_id and db.is_configured():
        try:
            session_mode = await asyncio.to_thread(db.get_session_genie_mode, session_id)
            if session_mode:
                return {"mode": session_mode}
        except Exception:
            pass
    return {"mode": "classic" if session_id else "agent"}


@router.post("/genie_mode")
async def set_genie_mode_endpoint(body: GenieModeRequest):
    """Set genie mode for a specific session."""
    if body.mode not in ("agent", "classic"):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="mode must be 'agent' or 'classic'")
    if body.session_id and db.is_configured():
        try:
            await asyncio.to_thread(db.set_session_genie_mode, body.session_id, body.mode)
        except Exception:
            pass
    return {"mode": body.mode, "session_id": body.session_id}


@router.get("/user")
async def get_user(request: Request):
    """Return the current user identity from forwarded headers."""
    return {
        "email": get_current_user(request),
        "name": get_current_user_name(request),
    }
