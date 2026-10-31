"""Operational load stats.

Exists so "how busy does this app actually get?" is one curl away. The alternative is
reaching Lakebase Postgres directly, which needs a notebook or a Postgres client — you
cannot point a SQL warehouse at it — so in practice the question would go unanswered
and any scaling decision would be guesswork.

Returns only our own operational counters, never fleet data, so it sits outside the
read-only analyst surface entirely.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query

from ..db import database as db
from ..services import turn_limiter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/load")
async def load(days: int = Query(7, ge=1, le=90)):
    """Current and historical turn load.

    `live` counters are process-local and reset on restart. `history` is derived from
    the executions table, so it survives restarts and covers the whole window.
    """
    out: dict = {"live": turn_limiter.stats()}

    if db.executions_available():
        try:
            out["history"] = await asyncio.to_thread(db.execution_load_stats, days)
        except Exception:
            logger.exception("load stats query failed")
            out["history"] = {"error": "query failed — see app logs"}
    else:
        out["history"] = {"error": "execution tables unavailable"}

    # Spell out the interpretation, because two things here are easy to misread: the
    # cap counts concurrent *turns* not users (a slot is held only while a turn runs,
    # ~60s, not while someone reads or types), and the history peak counts turns
    # in flight including any waiting for a slot, so it can exceed the cap.
    live = out["live"]
    hist = out.get("history") or {}
    out["reading"] = (
        f"cap is {live['cap']} concurrent turns, not users — a slot is held only while "
        f"a turn runs. Executing peak since restart: {live['peak_since_restart']}; "
        f"{live['turns_queued_since_restart']} turn(s) waited for a slot. "
        f"In-flight peak over {hist.get('window_days', '?')}d "
        f"(queued included, so it may exceed the cap): "
        f"{hist.get('peak_turns_in_flight', 'n/a')}."
    )
    return out
