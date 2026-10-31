"""Feedback endpoint."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..db import database as db
from ..services.user import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    level: str
    rating: str
    reason: Optional[str] = None
    comment: Optional[str] = None


@router.post("/feedback")
async def submit_feedback(request: Request, body: FeedbackRequest):
    user_email = get_current_user(request)
    feedback_id = None
    is_session = body.level == "session"

    # Session scores are numeric (1-5); answer votes stay "up"/"down".
    value: object = body.rating
    if is_session:
        try:
            value = int(body.rating)
        except (TypeError, ValueError):
            pass
    # Combine both halves so a chip plus free text reach the trace together; previously
    # picking a chip meant the typed comment never got there.
    rationale = " — ".join(p for p in (body.reason, body.comment) if p) or None

    # A session rating must anchor to the session's FIRST trace. This is not a shortcut:
    # MLflow's "Session assessments" panel is populated from that trace, so anchoring
    # anywhere else — including to a purpose-built session_rating trace — leaves the
    # rating out of the panel entirely and adds a phantom turn to the session. Measured
    # both ways in the UI. The client is not trusted with this: it used to send the
    # *latest* answer's trace, which is wrong in a different way.
    trace_id = body.trace_id
    if is_session and body.session_id and db.is_configured():
        trace_id = None
        try:
            meta = await asyncio.to_thread(db.get_session_meta, body.session_id)
            if meta and meta.get("first_trace_id"):
                trace_id = meta["first_trace_id"]
        except Exception:
            logger.exception("could not resolve first_trace_id for session feedback")

    if db.is_configured():
        try:
            feedback_id = await asyncio.to_thread(
                db.add_feedback,
                session_id=body.session_id,
                trace_id=trace_id,
                level=body.level,
                rater_email=user_email,
                rating=body.rating,
                reason=body.reason,
                comment=body.comment,
            )
        except Exception:
            logger.exception("Failed to store feedback")

    if trace_id:
        try:
            from ..tracing import log_feedback as mlflow_feedback

            # The reserved mlflow.trace.session key is what promotes this to a session
            # assessment rather than a verdict on the anchor trace's own answer.
            await asyncio.to_thread(
                mlflow_feedback,
                trace_id=trace_id,
                name="session_rating" if is_session else "answer_quality",
                value=value,
                rationale=rationale,
                source_id=user_email,
                metadata=({"mlflow.trace.session": str(body.session_id)} if is_session and body.session_id else None),
            )
        except Exception:
            logger.warning("MLflow feedback logging failed", exc_info=True)

    return {"success": True, "feedback_id": feedback_id}
