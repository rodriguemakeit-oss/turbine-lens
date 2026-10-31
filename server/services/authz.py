"""Session ownership checks.

Turbine Lens sessions are **readable by anyone** — the fleet is a shared concern and
the Gradio UI deliberately exposed a "Mine / All" filter so colleagues can learn
from each other's analyses. They are **writable only by their owner**.

This is a server-side boundary, not a UI nicety. Sending a turn into someone
else's session does more than append a message: the turn advances that session's
Claude CLI session id (`cli_session_id`), so the owner's own conversation state is
mutated underneath them. Renaming or deleting another user's session is likewise
destructive. Hiding the controls in React would leave the API wide open.

Read paths (`GET /sessions`, `GET /sessions/{id}`) are intentionally NOT guarded.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from ..db import database as db
from .user import get_current_user

logger = logging.getLogger(__name__)


async def session_owner(session_id: str) -> str | None:
    """Owner of `session_id`, or None if unknown."""
    if db.is_configured():
        try:
            meta = await asyncio.to_thread(db.get_session_meta, session_id)
            if meta and meta.get("owner_email"):
                return meta["owner_email"]
        except Exception:
            logger.exception("ownership lookup failed for session %s", session_id)
    return None


async def require_session_owner(request: Request, session_id: str) -> str:
    """Return the caller's email, or raise 403 if they don't own `session_id`.

    An unknown session is allowed through: it means nothing exists to protect
    yet (a brand-new id, or a session this process created before persistence
    came up). We only refuse when a *different* owner is on record.
    """
    caller = get_current_user(request)
    owner = await session_owner(session_id)
    if owner and owner != caller:
        logger.warning("refused write to session %s: caller=%s owner=%s", session_id, caller, owner)
        raise HTTPException(
            status_code=403,
            detail=f"This session belongs to {owner} and is read-only for you.",
        )
    return caller
