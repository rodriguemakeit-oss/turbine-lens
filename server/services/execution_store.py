"""Durable mirror of an in-flight turn's event stream.

A turn runs in an asyncio task and streams into `ActiveStream.events`, which is
in-process memory: fast, but it dies with the process and is invisible to a
browser that reloaded and lost its `execution_id`. This module mirrors those
events into Lakebase so that

  * a reloaded page can discover a turn already running and replay what it
    missed (see `GET /sessions/{id}/active_execution`), and
  * a turn killed by a restart leaves its partial output behind instead of
    disappearing.

**Batched, not per-event.** A turn emits an event per streamed chunk — 143 of
them for a 59s turn — so a database round-trip per event would dominate the
turn's own latency. We flush on a timer instead, which means a hard crash can
lose up to `FLUSH_INTERVAL_SECONDS` of the tail. That is the deliberate trade:
the alternative costs every turn to protect against a rare kill.

Every failure here is swallowed. Persistence is a safety net for the *viewer*;
it must never be able to kill the turn the user is waiting on.
"""

from __future__ import annotations

import asyncio
import logging

from ..db import database as db

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 5.0


async def create(stream) -> None:
    """Record the execution as running. Best-effort."""
    if not db.executions_available():
        return
    try:
        await asyncio.to_thread(db.create_execution, stream.execution_id, stream.session_id, stream.user_email)
    except Exception:
        logger.exception("could not create execution row for %s", stream.execution_id)


async def flush(stream) -> None:
    """Mirror any events not yet written. No-op when there is nothing pending."""
    if not db.executions_available():
        return
    start = stream.persisted_upto
    pending = list(stream.events[start:])
    if not pending:
        return
    try:
        stream.persisted_upto = await asyncio.to_thread(db.append_execution_events, stream.execution_id, start, pending)
    except Exception:
        # Leave persisted_upto alone so the next tick retries this batch; the
        # (execution_id, seq) key makes the retry idempotent.
        logger.exception("event flush failed for %s", stream.execution_id)


async def run_flush_loop(stream) -> None:
    """Flush on a timer until the turn ends, then flush the tail once more."""
    try:
        while not (stream.is_complete or stream.is_cancelled):
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await flush(stream)
    except asyncio.CancelledError:
        pass
    finally:
        await flush(stream)


async def finish(stream, status: str, error: str | None = None) -> None:
    """Flush the tail and record the terminal status."""
    await flush(stream)
    if not db.executions_available():
        return
    try:
        await asyncio.to_thread(db.finish_execution, stream.execution_id, status, error)
    except Exception:
        logger.exception("could not finalise execution %s", stream.execution_id)
