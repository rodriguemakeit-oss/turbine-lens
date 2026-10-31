"""Turbine Lens Genie evidence tools — one per space, free-text question interface.

Reuses Turbine Lens_App's genie_tool.py logic (start_conversation_and_wait +
attachment extraction) and adds `genie_sql` (the SQL Genie generated) so the
loop can cross-check with the read-data tools.

Each tool returns a JSON payload:
    { space, genie_sql, rows, text_summary, conversation_id }
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

from claude_agent_sdk import tool
from mcp.types import ToolAnnotations

from ..config import (
    GENIE_SPACES,
    GENIE_QUERY_TIMEOUT_SECONDS,
    GENIE_MAX_ATTEMPTS,
    GENIE_MAX_RESULT_ROWS,
    get_workspace_host,
)
from databricks.sdk import WorkspaceClient

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

logger = logging.getLogger(__name__)


def _extract(client, space_id: str, message) -> dict:
    """Pull generated SQL, result rows, and text summary from a Genie message."""
    rows: list[dict] = []
    text_summary = None
    genie_sql = None

    for att in message.attachments or []:
        if getattr(att, "text", None) and getattr(att.text, "content", None):
            text_summary = att.text.content

        q = getattr(att, "query", None)
        if not q:
            continue
        # The SQL Genie generated (enhancement vs Turbine Lens_App, which dropped it).
        genie_sql = getattr(q, "query", None) or genie_sql
        try:
            resp = client.genie.get_message_query_result_by_attachment(
                space_id=space_id,
                conversation_id=message.conversation_id,
                message_id=message.id,
                attachment_id=att.attachment_id,
            )
            stmt = resp.statement_response
            if stmt and stmt.manifest and stmt.manifest.schema and stmt.result:
                cols = [c.name for c in stmt.manifest.schema.columns]
                for row in stmt.result.data_array or []:
                    rows.append(dict(zip(cols, row)))
        except Exception as e:  # noqa: BLE001
            logger.warning("Genie result fetch failed for %s: %s", att.attachment_id, e)

    cap = GENIE_MAX_RESULT_ROWS
    if cap and len(rows) > cap:
        rows = rows[:cap]

    return {
        "space": space_id,
        "genie_sql": genie_sql,
        "rows": rows,
        "text_summary": text_summary,
        "conversation_id": getattr(message, "conversation_id", None),
    }


def _ask_genie_sync(space_id: str, question: str, conversation_id: str | None = None) -> dict:
    client = WorkspaceClient()
    timeout = timedelta(seconds=GENIE_QUERY_TIMEOUT_SECONDS)
    if conversation_id:
        # Continue an existing Genie conversation (multi-turn).
        msg = client.genie.create_message_and_wait(
            space_id=space_id,
            conversation_id=conversation_id,
            content=question,
            timeout=timeout,
        )
    else:
        # Start a fresh conversation.
        msg = client.genie.start_conversation_and_wait(
            space_id=space_id,
            content=question,
            timeout=timeout,
        )
    status = getattr(msg, "status", None)
    if status is not None and getattr(status, "value", None) == "FAILED":
        return {"space": space_id, "error": "Genie query returned FAILED status."}
    return _extract(client, space_id, msg)


async def _ask(space_key: str, args: dict[str, Any]) -> dict[str, Any]:
    question = (args.get("question") or "").strip()
    if not question:
        return {"content": [{"type": "text", "text": "Error: 'question' is required."}], "is_error": True}
    # Empty/missing conversation_id => start a fresh conversation.
    conversation_id = (args.get("conversation_id") or "").strip() or None
    space_id = GENIE_SPACES[space_key]
    try:
        res = await asyncio.to_thread(_ask_genie_sync, space_id, question, conversation_id)
        return {"content": [{"type": "text", "text": json.dumps(res, default=str)}]}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"Error ({type(e).__name__}): {e}"}], "is_error": True}


# Appended to every Genie tool description so the agent knows how to go multi-turn.
_MULTITURN = (
    " Omit `conversation_id` to start fresh; pass the `conversation_id` returned by a "
    "prior call to continue that same Genie conversation (multi-turn drill-down)."
)
_GENIE_SCHEMA = {"question": str, "conversation_id": str}


@tool(
    "genie_rema_qualitative_context",
    "Ask the REMA qualitative-context Genie space about reported issues/events — the "
    "OPERATIONAL side: what was observed or written about a problem on a site, asset or "
    "component, the initiatives and actions tracked against it (status, priority, assignees, "
    "deadline, closure), and REMA's own estimate of the event's energy impact in MWh. Use for "
    "questions like 'what actions are overdue?'. Complements genie_rex_qualitative_context "
    "(REX): the two cover the SAME plants from different angles — REMA is the operational "
    "problem and what is being done about it, REX is the lesson learned for reuse — so combine "
    "them when a question spans both. They link on plant_name, but the strings differ in the "
    "CASE of the technology suffix (REMA 'Plourin Wind' vs REX 'Plourin WIND'), so match "
    "case-insensitively; an exact-string join finds almost nothing. CAUTION on REMA's MWh "
    "figure: it is a human-entered ESTIMATE of an event's impact — signed (a gain is possible), "
    "frequently zero, and covering solar as well as wind. It is NOT validated energy loss. "
    "Report it as REMA's recorded estimate, attributed as such; never present it as the loss, "
    "never sum it, and never put it beside a wind_stops figure without labelling which is "
    "which. Returns Genie's "
    "generated SQL + result rows + a text summary. Pass a natural-language `question`." + _MULTITURN,
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_rema_qualitative_context(args):
    return await _ask("rema_qualitative_context", args)


@tool(
    "genie_rex_qualitative_context",
    "Ask the REX (Return of Experience) Genie space about lessons learned — a knowledge base "
    "of ~1,000 structured lessons from past events on renewable assets (wind, solar, hydro, "
    "BESS). USE for: what went wrong or well on past projects or operations; lessons learned, "
    "corrective actions and best practices from fleet events; impact assessment of past events "
    "(CAPEX, OPEX, schedule delay, revenue loss, HSE, reputation); REX alerts or urgent lessons "
    "needing attention; event history by plant, country, technology, supplier, project phase or "
    "knowledge area; and whether a lesson is relevant at entity, country, GBU or portfolio-ui "
    "level. Complements genie_rema_qualitative_context (REMA): the two cover the SAME plants "
    "from different angles — REMA is the operational problem, its tracked actions and its MWh "
    "impact estimate; REX is the knowledge kept for future reference — so combine them when a "
    "question spans both. They link on plant_name, but the strings differ in the CASE of the "
    "technology suffix (REMA 'Plourin Wind' vs REX 'Plourin WIND'), so match case-insensitively; "
    "an exact-string join finds almost nothing. DO NOT use "
    "for real-time or operational data (production metrics, energy output, availability, alarms, "
    "SCADA) or for financial reporting. CRITICAL: any energy- or production-loss figure "
    "appearing in REX is narrative and INFORMATIVE ONLY — never a validated loss. Do not quote "
    "it as the loss, and never compute loss from it; validated energy loss comes exclusively "
    "from the wind_stops table. Returns Genie's generated SQL + result rows + a text "
    "summary. Pass a natural-language `question`." + _MULTITURN,
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_rex_qualitative_context(args):
    return await _ask("rex_qualitative_context", args)


@tool(
    "genie_lorin_qualitative_context",
    "Ask the Lorinlog Events Genie space about events logged against fleet assets — the "
    "event log itself: what is open or closed on an asset, how severe it is, its risk "
    "tier, the narrative of the incident, and when it returned to service. USE for 'what "
    "is open on X', 'which assets have HIGH severity events', or an asset's event "
    "history. Covers ALL technologies, not just wind. Complements "
    "genie_rema_qualitative_context (the initiatives and actions tracked against a "
    "problem) and genie_rex_qualitative_context (the lesson kept for reuse): Lorinlog is "
    "the log entry itself, so combine them when a question spans what happened, what is "
    "being done about it, and what was learned. Returns SQL + rows + summary." + _MULTITURN,
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_lorin_qualitative_context(args):
    return await _ask("lorin_qualitative_context", args)


@tool(
    "genie_anomaly_detection",
    "Ask the anomaly-detection Genie space whether the prediction-vs-observation algorithm "
    "flagged an anomaly, or for quantitative evidence of a component problem. This is the "
    "only anomaly source. Returns SQL + rows + summary." + _MULTITURN,
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_anomaly_detection(args):
    return await _ask("anomaly_detection", args)


@tool(
    "genie_alarm_stop_correlation",
    "Ask the alarm/stop-correlation Genie space how turbines behaved (stops and alarms) "
    "around an event such as a grid event, maintenance, or weather. Returns SQL + rows + summary." + _MULTITURN,
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_alarm_stop_correlation(args):
    return await _ask("alarm_stop_correlation", args)


@tool(
    "genie_fleet_reporting",
    "Ask the 'Datasquare Fleet perf KPIs' Genie space for official wind-fleet reporting KPIs "
    "at site/unit level — net generation, planned/unplanned unavailability (MWh & hours), "
    "curtailment, installed capacity, wind index — including BUDGET-vs-ACTUAL and forecast "
    "(plans & Net-Zero trajectories, via the `version` dimension), sliced by country, business "
    "unit, technology, or period. Use for reporting roll-ups, site-level KPI analysis, budget & "
    "previsions, or when the other spaces/tables have nothing. Returns SQL + rows + summary." + _MULTITURN,
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_fleet_reporting(args):
    return await _ask("fleet_reporting", args)


GENIE_TOOLS = [
    genie_rema_qualitative_context,
    genie_rex_qualitative_context,
    genie_lorin_qualitative_context,
    genie_anomaly_detection,
    genie_alarm_stop_correlation,
    genie_fleet_reporting,
]
# These strings gate the agent's access: a name here that does not match a @tool name
# above makes the tool silently unavailable rather than erroring, so they must be kept
# in step with GENIE_TOOLS. The test at the bottom of this list is simply length.
GENIE_TOOL_NAMES = [
    "mcp__databricks__genie_rema_qualitative_context",
    "mcp__databricks__genie_rex_qualitative_context",
    "mcp__databricks__genie_lorin_qualitative_context",
    "mcp__databricks__genie_anomaly_detection",
    "mcp__databricks__genie_alarm_stop_correlation",
    "mcp__databricks__genie_fleet_reporting",
]
