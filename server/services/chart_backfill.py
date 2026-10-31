"""One-shot recovery of charts drawn before chart specs were mirrored to Lakebase.

Those specs looked lost: the payload only ever existed under `TURBINE_LENS_CHART_DIR`, and
every restart wipes it. But `tracing.log_turn_trace` records each tool call's **input**
verbatim — and the input to `render_echarts` / `render_chart` *is* the chart spec. So the
last copy of a "lost" chart is the MLflow trace of the turn that drew it.

**Positional pairing, and its limit.** `tracing.extract_output` has no render branch, so
the filename the tool returned was never recorded — the span says only
`{"status": "ok"}`. The link is therefore "nth fence in the answer ↔ nth render span in
the trace", which is safe only when the counts agree. An answer whose counts differ (the
agent drew three charts and published two) is **skipped**, not guessed at: a chart under
the wrong heading is worse than a chart that is missing.

Opt-in and inert by default, because it exists to be run twice and then forgotten:

    TURBINE_LENS_CHART_BACKFILL=dry     log the fence → title mapping, write nothing
    TURBINE_LENS_CHART_BACKFILL=write   upsert the specs
    (unset)                          no-op

Runs in a background task: it makes one HTTP call per traced answer and must never sit in
front of the Apps startup probe. Every failure is swallowed — recovering old charts cannot
be allowed to affect a booting app.
"""

from __future__ import annotations

import asyncio
import logging
import os

from ..db import database as db
from .chart_store import names_in

logger = logging.getLogger(__name__)

_TRACE_ARTIFACT_PATH = "/api/2.0/mlflow/get-trace-artifact"


def mode() -> str:
    """ "dry", "write", or "" (disabled)."""
    value = (os.environ.get("TURBINE_LENS_CHART_BACKFILL") or "").strip().lower()
    return value if value in ("dry", "write") else ""


def _title_of(spec: dict) -> str | None:
    """A human-readable label, purely so the dry run is reviewable."""
    if not isinstance(spec, dict):
        return None
    title = spec.get("title")
    if isinstance(title, list) and title:
        title = title[0]
    if isinstance(title, dict):
        return title.get("text")
    layout = spec.get("layout")  # plotly
    if isinstance(layout, dict) and layout.get("title"):
        t = layout["title"]
        return t.get("text") if isinstance(t, dict) else str(t)
    return None


def _render_specs(trace_id: str) -> list[dict]:
    """Chart specs from a trace's render spans, in call order.

    Uses the REST endpoint rather than `mlflow.get_trace` because span *inputs* are not in
    the trace-info response, and the artifact path is not readable outside the workspace.
    """
    import json

    from databricks.sdk import WorkspaceClient

    payload = WorkspaceClient().api_client.do("GET", _TRACE_ARTIFACT_PATH, query={"request_id": trace_id})
    specs = []
    for span in (payload or {}).get("spans") or []:
        if "render" not in (span.get("name") or ""):
            continue
        raw = (span.get("attributes") or {}).get("mlflow.spanInputs")
        if not raw:
            continue
        try:
            inputs = json.loads(raw)
        except Exception:  # noqa: BLE001
            logger.warning("trace %s: unparseable span inputs (%d bytes)", trace_id, len(raw))
            continue
        spec = inputs.get("option") or inputs.get("figure")
        if isinstance(spec, str):
            # Some models pass the spec as a JSON string; the render tools coerce it
            # (mcp_tools._render_*), so the trace holds whichever form the model sent.
            # Missing this silently dropped 4 real charts on the first dry run.
            try:
                spec = json.loads(spec)
            except Exception:  # noqa: BLE001
                logger.warning("trace %s: spec string is not JSON", trace_id)
                continue
        if isinstance(spec, dict):
            specs.append(spec)
    return specs


async def run() -> None:
    """Recover what can be recovered. Reports a summary; never raises."""
    how = mode()
    if not how:
        return
    if not db.charts_available():
        logger.warning("chart backfill skipped: chart_specs table unavailable")
        return

    logger.info("chart backfill starting (mode=%s)", how)
    mapped = written = skipped = present = 0
    try:
        rows = await asyncio.to_thread(db.chart_messages)
    except Exception:
        logger.exception("chart backfill: could not read messages")
        return

    for row in rows:
        names = names_in(row["content"])
        if not names:
            continue
        try:
            have = await asyncio.to_thread(db.existing_chart_names, names)
        except Exception:
            logger.exception("chart backfill: could not check existing specs")
            return
        if len(have) == len(names):
            present += len(names)
            continue

        trace_id = row["trace_id"]
        try:
            specs = await asyncio.to_thread(_render_specs, trace_id)
        except Exception:
            logger.warning("chart backfill: trace %s unreadable", trace_id, exc_info=True)
            skipped += len(names)
            continue

        if len(specs) != len(names):
            # Counts disagree, so position no longer identifies a chart. Report loudly
            # enough to be hand-mapped later if those charts matter.
            logger.warning(
                "chart backfill SKIP trace=%s fences=%d render_spans=%d files=%s",
                trace_id,
                len(names),
                len(specs),
                names,
            )
            skipped += len(names)
            continue

        for name, spec in zip(names, specs):
            if name in have:
                present += 1
                continue
            mapped += 1
            logger.info("chart backfill %s %s <- %s", "WRITE" if how == "write" else "DRY  ", name, _title_of(spec))
            if how == "write":
                try:
                    await asyncio.to_thread(db.upsert_chart_spec, name, spec)
                    written += 1
                except Exception:
                    logger.exception("chart backfill: could not write %s", name)
                    skipped += 1

    logger.info(
        "chart backfill done (mode=%s): recovered=%d written=%d already_present=%d skipped=%d",
        how,
        mapped,
        written,
        present,
        skipped,
    )
