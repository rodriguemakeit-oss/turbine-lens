"""Bound on how many agent turns run at once.

Every turn spawns a Claude Code CLI subprocess, so unbounded turns exhaust the
container. The Gradio app capped this at 5 deliberately — "we bound it rather than
let unlimited turns exhaust the instance" — and the React/FastAPI rewrite dropped the
cap entirely, so N simultaneous users meant N subprocesses with nothing counting them.

**We queue rather than reject.** `invoke_agent` returns an `execution_id` before any
work starts, so a turn can wait for a slot while the client is already streaming: the
user sees "waiting for a free slot" instead of a 429, and no client-side retry logic
is needed. This is also better than the Gradio behaviour, where excess turns queued
invisibly inside Gradio's own machinery.

The cap is an env var so it can be tuned in `app.yml` without a rebuild. 5 is the
inherited default, and it was a judgement call paired with `compute_size: LARGE`, not
a measured ceiling — which is what the counters here are for. Note the unit is
concurrent *turns*, not users: a user occupies a slot only while a turn is actually
running (~60s), not while reading or typing.

Counters are process-local and reset on restart, deliberately: recording queue waits
durably would mean a schema change, and `ALTER` needs table ownership that a
recreated app's service principal would not have. Peak *concurrency* is derivable
from the `executions` table instead, which already records start and end times.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Callable

logger = logging.getLogger(__name__)

MAX_CONCURRENT_TURNS = max(1, int(os.environ.get("TURBINE_LENS_MAX_CONCURRENT_TURNS", "5")))

_sem = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
_in_flight = 0
_peak = 0
_queued_total = 0
_wait_ms_total = 0.0
_started_total = 0
_since = time.time()


@asynccontextmanager
async def turn_slot(on_queued: Callable[[int], None] | None = None):
    """Hold a turn slot for the duration of the block. Yields the wait in ms.

    `on_queued` is called with the number of turns ahead when a wait is likely, so the
    caller can tell the user why nothing is happening yet.
    """
    global _in_flight, _peak, _queued_total, _wait_ms_total, _started_total

    t0 = time.monotonic()
    will_wait = _in_flight >= MAX_CONCURRENT_TURNS
    if will_wait:
        _queued_total += 1
        ahead = _in_flight - MAX_CONCURRENT_TURNS + 1
        logger.info(
            "turn queued: %d in flight (cap %d), %d ahead",
            _in_flight,
            MAX_CONCURRENT_TURNS,
            ahead,
        )
        if on_queued:
            try:
                on_queued(ahead)
            except Exception:
                logger.debug("on_queued callback failed", exc_info=True)

    async with _sem:
        waited_ms = (time.monotonic() - t0) * 1000.0
        _wait_ms_total += waited_ms
        _started_total += 1
        _in_flight += 1
        if _in_flight > _peak:
            _peak = _in_flight
            logger.info("concurrent-turn high-water mark: %d (cap %d)", _peak, MAX_CONCURRENT_TURNS)
        if will_wait:
            logger.info("turn dequeued after %.0f ms", waited_ms)
        try:
            yield waited_ms
        finally:
            _in_flight -= 1


def in_flight() -> int:
    return _in_flight


def stats() -> dict:
    """Process-local load counters. Reset on restart — see the module docstring."""
    return {
        "cap": MAX_CONCURRENT_TURNS,
        "in_flight": _in_flight,
        "peak_since_restart": _peak,
        "turns_started_since_restart": _started_total,
        "turns_queued_since_restart": _queued_total,
        "avg_queue_wait_ms": round(_wait_ms_total / _started_total, 1) if _started_total else 0.0,
        "counters_since": _since,
    }
