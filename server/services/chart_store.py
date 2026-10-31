"""Durable mirror of a rendered chart's spec.

The chart tools write a spec to `TURBINE_LENS_CHART_DIR` and hand the agent a fence
containing that **path**, which then lives in the answer text. The answer is durable and
the file is not: Databricks Apps containers are replaced on every deploy or restart, so
every chart in every older conversation became a 404 iframe — the same durable-pointer /
ephemeral-payload bug as resuming a CLI transcript (see services/history.py).

So Lakebase is the source of truth and the local file is a read-through cache: written on
render, re-warmed on a miss, and never required to exist.

Every failure here is swallowed. A chart is worth less than the turn producing it, and
less than the answer it sits inside — losing durability must never fail a render.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..db import database as db
from .history import CHART_FENCE_RE

logger = logging.getLogger(__name__)


def names_in(text: str) -> list[str]:
    """Bare chart filenames referenced by an answer's fences.

    The fence carries a full path but the table is keyed by filename, which is also all
    the `/charts/{filename}` route ever sees.
    """
    if not text:
        return []
    return [Path(raw.strip()).name for raw in CHART_FENCE_RE.findall(text) if raw.strip()]


async def save(name: str, spec: dict) -> None:
    """Mirror a freshly rendered chart. Best-effort."""
    if not db.charts_available():
        return
    try:
        await asyncio.to_thread(db.upsert_chart_spec, name, spec)
    except Exception:
        logger.exception("could not mirror chart spec %s", name)


async def load(name: str) -> dict | None:
    """The durable copy of a chart whose file is gone. Best-effort."""
    if not db.charts_available():
        return None
    try:
        return await asyncio.to_thread(db.get_chart_spec, name)
    except Exception:
        logger.exception("could not load chart spec %s", name)
        return None


async def delete_for_session(session_id: str) -> None:
    """Drop the specs belonging to a session that is being deleted.

    Chart rows carry no session id — the render tool runs inside the agent loop and has
    no session context to record — so the link is the fences in the session's own
    answers. That means this must run *before* the messages are deleted, and it is
    best-effort: an orphaned spec is harmless, whereas failing here would block the
    delete the user actually asked for.
    """
    if not db.charts_available():
        return
    try:
        messages = await asyncio.to_thread(db.get_session_messages, session_id)
        names = [n for m in messages for n in names_in(m.get("content") or "")]
        if names:
            removed = await asyncio.to_thread(db.delete_chart_specs, names)
            logger.info("deleted %d chart spec(s) for session %s", removed, session_id)
    except Exception:
        logger.exception("chart cleanup failed for session %s", session_id)
