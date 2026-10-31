"""Session report endpoints.

Generating via this endpoint is open to session readers. It appends an immutable
report version attributed to the caller, without modifying messages or session state.
Sending a turn (including a report request via chat) remains owner-only.

Reading is open, like sessions themselves: a report is a view of a conversation everyone can
already read, and the point of the document is to be shared.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..db import database as db
from ..services import report_generator
from ..services.user import get_current_user, get_fmapi_credentials

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sessions/{session_id}/report")
async def create_report(request: Request, session_id: str):
    """Generate a report snapshot for any readable session, attributed to the caller."""
    if not db.reports_available():
        raise HTTPException(status_code=503, detail="Report storage is not available.")

    host, token = get_fmapi_credentials()
    try:
        result = await asyncio.to_thread(report_generator.generate, session_id, get_current_user(request), host, token)
    except report_generator.ReportError as exc:
        # The user asked for this document, so a failure has to be visible rather than
        # swallowed the way a chart or a trace failure is.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("report generation failed for session %s", session_id)
        raise HTTPException(status_code=500, detail="Report generation failed.") from exc
    return result


@router.get("/sessions/{session_id}/reports")
async def list_reports(session_id: str):
    """Every version for this session, newest first. No HTML — this feeds a picker."""
    if not db.reports_available():
        return []
    try:
        return await asyncio.to_thread(db.list_reports, session_id)
    except Exception:
        logger.exception("list_reports failed for %s", session_id)
        return []


@router.get("/reports/{report_id}")
async def get_report(report_id: str):
    """The frozen document, exactly as it was rendered when this version was created."""
    if not db.reports_available():
        raise HTTPException(status_code=503, detail="Report storage is not available.")
    row = await asyncio.to_thread(db.get_report, report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    return HTMLResponse(content=row["html"])
