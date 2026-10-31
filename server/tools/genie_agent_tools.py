"""Turbine Lens Genie evidence tools — Agent Mode API.

Uses the Genie Agent Mode API (POST /api/2.0/genie/agents/{agent_id}/responses)
with SSE streaming + polling (GET .../conversations/{id}/items) to leverage
Genie's multi-step research plan: reasoning → SQL queries → iterates → final
report with citations.

Drop-in alternative to genie_tools.py.  Switch imports in the server entrypoint
to use Agent Mode instead of the classic conversation API.

Each tool returns a JSON payload:
    { space, conversation_id, reasoning, queries: [{sql, title, rows_file, rows_count}], report }

Query rows are persisted to JSON files under TURBINE_LENS_RESULTS_DIR (default /tmp/turbine_lens_genie_results)
and referenced via `rows_file` path. No row cap is applied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import requests as _requests
from claude_agent_sdk import tool
from mcp.types import ToolAnnotations

from ..config import (
    GENIE_SPACES,
    GENIE_QUERY_TIMEOUT_SECONDS,
    GENIE_AGENT_POLL_INTERVAL_SECONDS,
)
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent Mode API base path
# ---------------------------------------------------------------------------
_AGENT_API_BASE = "/api/2.0/genie/agents"

# Directory for persisting query result rows as JSON files.
_RESULTS_DIR = Path(os.environ.get("TURBINE_LENS_RESULTS_DIR", "/tmp/turbine_lens_genie_results"))
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _save_rows_to_file(rows: list[dict], space_id: str, query_index: int) -> str:
    """Persist rows to a JSON file and return the file path."""
    filename = f"{space_id}_{uuid.uuid4().hex[:12]}_{query_index}.json"
    filepath = _RESULTS_DIR / filename
    filepath.write_text(json.dumps(rows, default=str, ensure_ascii=False), encoding="utf-8")
    return str(filepath)


# ---------------------------------------------------------------------------
# Helpers: auth, SSE consumption, item retrieval
# ---------------------------------------------------------------------------


def _get_auth_headers(client: WorkspaceClient) -> dict[str, str]:
    """Extract authorization headers from the SDK's configured auth.

    Handles multiple databricks-sdk versions:
      - Newer: authenticate() returns Dict[str, str] directly
      - Older: authenticate() returns a HeaderFactory (Callable[[], Dict[str, str]])
    """
    result = client.config.authenticate()
    if callable(result):
        return result()
    return result


def _create_response_sse(
    client: WorkspaceClient,
    agent_id: str,
    question: str,
    conversation_id: str | None = None,
) -> tuple[str, list[dict]]:
    """POST to Agent Mode via streaming SSE.

    Uses stream=True and iter_lines to parse events incrementally.
    Collects output items from `response.output_item.done` events so the
    caller can skip the GET /items polling phase entirely.

    Returns:
        (conversation_id, items) — items may be empty if the SSE stream
        did not emit output_item events (caller should fallback to _get_items).
    """
    host = client.config.host.rstrip("/")
    url = f"{host}{_AGENT_API_BASE}/{agent_id}/responses"
    body: dict[str, Any] = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": question}],
            }
        ],
    }
    if conversation_id:
        body["conversation_id"] = conversation_id

    auth_headers = _get_auth_headers(client)
    resp = _requests.post(
        url,
        json=body,
        headers={**auth_headers, "Accept": "text/event-stream"},
        timeout=GENIE_QUERY_TIMEOUT_SECONDS,
        stream=True,
    )
    resp.raise_for_status()

    # --- Non-SSE fallback (plain JSON response) ---
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        try:
            json_body = resp.json()
        except Exception:
            raw = resp.text[:500] if hasattr(resp, "text") else "(no body)"
            raise RuntimeError(
                f"Genie Agent Mode returned unexpected content-type '{content_type}'. Body preview: {raw}"
            )
        conv_id = (
            json_body.get("conversation_id")
            or json_body.get("conversation", {}).get("id")
            or json_body.get("message", {}).get("conversation_id")
        )
        if not conv_id:
            raise RuntimeError(
                f"Genie Agent Mode returned JSON (not SSE). Response: {json.dumps(json_body, default=str)[:1000]}"
            )
        return conv_id, []

    # --- Stream SSE line-by-line (stream=True) ---
    # SSE is UTF-8 by spec, but `requests` decodes any `text/*` response without a
    # charset as ISO-8859-1. That turned every non-ASCII character Genie wrote into
    # mojibake ("Widehem Wind Farm — …" -> "Widehem Wind Farm â\x80\x94 …").
    resp.encoding = "utf-8"
    conv_id: str | None = conversation_id
    items: list[dict] = []
    final_status: str | None = None
    error_info: dict | None = None
    data_parts: list[str] = []

    def _flush_event() -> None:
        """Parse accumulated data_parts as one SSE event."""
        nonlocal conv_id, final_status, error_info
        if not data_parts:
            return
        full_data = "\n".join(data_parts)
        data_parts.clear()
        try:
            event_data = json.loads(full_data)
        except (json.JSONDecodeError, TypeError):
            logger.debug("SSE: could not parse event data: %s", full_data[:200])
            return

        event_type = event_data.get("type", "")

        if event_type == "response.created":
            resp_obj = event_data.get("response", {})
            conv_id = resp_obj.get("conversation_id") or conv_id

        elif event_type == "response.completed":
            final_status = "completed"
            resp_obj = event_data.get("response", {})
            conv_id = resp_obj.get("conversation_id") or conv_id

        elif event_type == "response.failed":
            final_status = "failed"
            resp_obj = event_data.get("response", {})
            error_info = resp_obj.get("error")
            conv_id = resp_obj.get("conversation_id") or conv_id

        elif event_type == "response.output_item.done":
            item = event_data.get("item")
            if item:
                items.append(item)

    for raw_line in resp.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        line = raw_line.rstrip("\r\n")

        if line == "":
            # Empty line = SSE event boundary.
            _flush_event()
            continue

        if line.startswith("data:"):
            payload = line[5:]
            if payload.startswith(" "):
                payload = payload[1:]
            data_parts.append(payload)

    # Flush any trailing event (stream may end without a final blank line).
    _flush_event()

    if final_status == "failed":
        msg = error_info.get("message", "Unknown error") if error_info else "Unknown error"
        raise RuntimeError(f"Genie Agent Mode failed: {msg}")

    if not conv_id:
        raise RuntimeError("No conversation_id found in Agent Mode SSE stream.")

    return conv_id, items


def _get_items(client: WorkspaceClient, agent_id: str, conversation_id: str) -> list[dict]:
    """GET conversation items with pagination.

    Polls the top-level status until completed/failed, then paginates to
    collect all output items.
    """
    host = client.config.host.rstrip("/")
    url = f"{host}{_AGENT_API_BASE}/{agent_id}/conversations/{conversation_id}/items"
    auth_headers = _get_auth_headers(client)

    # Poll until terminal status.
    deadline = time.monotonic() + GENIE_QUERY_TIMEOUT_SECONDS
    payload: dict = {}
    while True:
        resp = _requests.get(
            url,
            headers=auth_headers,
            params={"limit": 100, "order": "asc"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        status = payload.get("status")
        if status in ("completed", "failed", None):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"Genie Agent Mode did not complete within {GENIE_QUERY_TIMEOUT_SECONDS}s")
        time.sleep(GENIE_AGENT_POLL_INTERVAL_SECONDS)

    # Paginate remaining items.
    all_items: list[dict] = payload.get("data", [])
    after = payload.get("last_id")
    has_more = payload.get("has_more", False)

    while has_more and after:
        resp = _requests.get(
            url,
            headers=auth_headers,
            params={"limit": 100, "order": "asc", "after": after},
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()
        all_items.extend(page.get("data", []))
        has_more = page.get("has_more", False)
        after = page.get("last_id")

    return all_items


# ---------------------------------------------------------------------------
# Parsing: convert raw output items into structured result
# ---------------------------------------------------------------------------


def _metadata_to_rows(metadata: dict) -> list[dict]:
    """Convert table-chunk metadata (columns + preview_rows) to list-of-dicts."""
    columns = metadata.get("columns") or []
    preview_rows = metadata.get("preview_rows") or []
    col_names = [c.get("name", f"_c{i}") for i, c in enumerate(columns)]
    rows: list[dict] = []
    for row_values in preview_rows:
        rows.append(dict(zip(col_names, row_values)))
    return rows


def _parse_output_items(items: list[dict]) -> dict:
    """Parse Agent Mode output items into a structured result.

    Output item types handled:
      - reasoning: Genie's internal research plan
      - function_call: SQL query the agent runs
      - function_call_output: query results (paired via call_id)
      - message (role=assistant): final report with inline tables & citations
    """
    reasoning_parts: list[str] = []
    queries: list[dict] = []
    report: str | None = None
    call_map: dict[str, dict] = {}  # call_id -> {sql, title, rows}

    for item in items:
        item_type = item.get("type")

        if item_type == "reasoning":
            for content_item in item.get("content") or []:
                if content_item.get("type") == "reasoning_text":
                    reasoning_parts.append(content_item.get("text", ""))

        elif item_type == "function_call":
            call_id = item.get("call_id", "")
            args_str = item.get("arguments", "{}")
            try:
                args_parsed = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                args_parsed = {}
            call_map[call_id] = {
                "sql": args_parsed.get("sql"),
                "title": args_parsed.get("title"),
                "rows": [],
            }

        elif item_type == "function_call_output":
            call_id = item.get("call_id", "")
            output_text = item.get("output", "")
            if call_id in call_map:
                call_map[call_id]["output_raw"] = output_text

        elif item_type == "message":
            if item.get("role") == "assistant":
                content_parts = item.get("content") or []
                report_chunks: list[str] = []
                for chunk in content_parts:
                    if chunk.get("type") != "output_text":
                        continue
                    text = chunk.get("text", "")
                    metadata = chunk.get("metadata")
                    report_chunks.append(text)
                    # If metadata has structured rows, attach to matching query.
                    if metadata and metadata.get("sql"):
                        sql_key = metadata["sql"]
                        rows = _metadata_to_rows(metadata)
                        matched = False
                        for _cid, qinfo in call_map.items():
                            if qinfo.get("sql") == sql_key:
                                qinfo["rows"] = rows
                                matched = True
                                break
                        if not matched:
                            queries.append({"sql": sql_key, "title": None, "rows": rows})
                report = "\n".join(report_chunks) if report_chunks else None

    # Append call_map entries to queries list.
    for _call_id, qinfo in call_map.items():
        queries.append(
            {
                "sql": qinfo.get("sql"),
                "title": qinfo.get("title"),
                "rows": qinfo.get("rows", []),
            }
        )

    return {
        "reasoning": "\n".join(reasoning_parts) if reasoning_parts else None,
        "queries": queries,
        "report": report,
    }


# ---------------------------------------------------------------------------
# Orchestrator: ties SSE + items + parsing together
# ---------------------------------------------------------------------------


def _ask_genie_agent_sync(space_id: str, question: str, conversation_id: str | None = None) -> dict:
    """Call the Genie Agent Mode API synchronously (blocking).

    1. POST /responses — SSE stream, consume to get conversation_id + completion.
    2. GET /conversations/{id}/items — paginated retrieval of all output items.
    3. Parse into structured result.
    4. Offload query rows to JSON files under _RESULTS_DIR (uncapped).
    """
    client = WorkspaceClient()

    conv_id, items = _create_response_sse(client, space_id, question, conversation_id)

    # Fallback: if SSE didn't yield output items, poll the items endpoint.
    if not items:
        logger.info("SSE yielded no items for conv %s; falling back to _get_items", conv_id)
        items = _get_items(client, space_id, conv_id)

    parsed = _parse_output_items(items)

    # Offload rows to temp JSON files to keep the response payload lightweight.
    for idx, query_info in enumerate(parsed.get("queries", [])):
        rows = query_info.pop("rows", [])
        if rows:
            query_info["rows_file"] = _save_rows_to_file(rows, space_id, idx)
            query_info["rows_count"] = len(rows)
        else:
            query_info["rows_file"] = None
            query_info["rows_count"] = 0

    return {
        "space": space_id,
        "conversation_id": conv_id,
        **parsed,
    }


async def _ask(space_key: str, args: dict[str, Any]) -> dict[str, Any]:
    """Async wrapper: validate input, dispatch to sync Agent Mode call in a thread."""
    question = (args.get("question") or "").strip()
    if not question:
        return {
            "content": [{"type": "text", "text": "Error: 'question' is required."}],
            "is_error": True,
        }
    conversation_id = (args.get("conversation_id") or "").strip() or None
    space_id = GENIE_SPACES[space_key]
    try:
        res = await asyncio.to_thread(_ask_genie_agent_sync, space_id, question, conversation_id)
        return {"content": [{"type": "text", "text": json.dumps(res, default=str)}]}
    except Exception as e:  # noqa: BLE001
        return {
            "content": [{"type": "text", "text": f"Error ({type(e).__name__}): {e}"}],
            "is_error": True,
        }


# ---------------------------------------------------------------------------
# Tool definitions (Agent Mode variants)
# ---------------------------------------------------------------------------

_MULTITURN = (
    " Omit `conversation_id` to start fresh; pass the `conversation_id` returned by a "
    "prior call to continue that same Genie conversation (multi-turn drill-down). "
    "Agent Mode returns: reasoning (Genie's research plan), queries (SQL + rows), "
    "and a final report with citations."
)
_GENIE_SCHEMA = {"question": str, "conversation_id": str}
_READ_ONLY = ToolAnnotations(readOnlyHint=True)

# What a space COVERS comes from the space's own description, read from the Genie REST
# API — so changing a space needs no code edit. What stays in code (`guardrails`) is
# Turbine Lens policy that must not depend on whoever can edit a space: cross-space routing
# and the energy-loss caveats (CLAUDE.md invariant #4). It is placed AFTER the space's
# text so policy has the last word. `fallback` covers a failed call or an empty
# description, so a tool is never left undescribed and the app never fails to start.
#
# Fetched ONCE, when this module is first imported (the first agent turn, via
# mcp_tools.get_tool_server): the @tool decorators below freeze the text. A space edited
# afterwards reaches the agent on the next process restart (`bundle run`).
_DESC_MAX_CHARS = 30000  # sent on every turn; an unbounded space description must not bloat it
_DESC_TIMEOUT_SECONDS = 30  # import-time and blocking: a slow API must only cost the fallback
_RESEARCH = (
    "Pass a natural-language `question`. Genie runs a multi-step research plan and "
    "returns a report with SQL evidence."
)


def _fetch_space(space_id: str) -> dict:
    """GET /api/2.0/genie/spaces/{id} — the space's metadata, incl. title and description."""
    client = WorkspaceClient()
    resp = _requests.get(
        f"{client.config.host.rstrip('/')}/api/2.0/genie/spaces/{space_id}",
        headers=_get_auth_headers(client),
        timeout=_DESC_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


def _describe(space_key: str, fallback: str, guardrails: str = "") -> str:
    """Tool description: the space's own description (else `fallback`), then guardrails.

    `space_key` is a key of config.GENIE_SPACES.
    """
    space_id = GENIE_SPACES[space_key]
    try:
        meta = _fetch_space(space_id)
    except Exception as e:  # noqa: BLE001 — a description must never stop the tools loading
        logger.warning("Genie space %s (%s): description fetch failed, using fallback: %s", space_key, space_id, e)
        meta = {}
    desc = str(meta.get("description") or "").strip()
    if desc:
        if len(desc) > _DESC_MAX_CHARS:
            logger.warning("Genie space %s: description truncated to %d chars", space_key, _DESC_MAX_CHARS)
            desc = desc[:_DESC_MAX_CHARS].rstrip() + " …"
        title = str(meta.get("title") or "").strip()
        base = f"Ask the Genie space '{title}'.\n{desc}" if title else desc
        logger.info("Genie space %s: tool description taken from the space", space_key)
    else:
        if meta:
            logger.warning("Genie space %s (%s) has no description; using fallback", space_key, space_id)
        base = fallback
    return "\n\n".join(p for p in (base, guardrails, _RESEARCH + _MULTITURN) if p)


_REMA_REX_LINK = (
    "They link on plant_name, but the strings differ in the CASE of the technology suffix "
    "(REMA 'Plourin Wind' vs REX 'Plourin WIND'), so match case-insensitively; an "
    "exact-string join finds almost nothing."
)


@tool(
    "genie_rema_qualitative_context",
    _describe(
        "rema_qualitative_context",
        fallback=(
            "Ask the REMA qualitative-context Genie space about reported issues/events — the "
            "OPERATIONAL side: what was observed or written about a problem on a site, asset or "
            "component, the initiatives and actions tracked against it (status, priority, "
            "assignees, deadline, closure), and REMA's own estimate of the event's energy impact "
            "in MWh. Use for questions like 'what actions are overdue?'."
        ),
        guardrails=(
            "Complements genie_rex_qualitative_context (REX): the two cover the SAME plants "
            "from different angles — REMA is the operational problem and what is being done "
            "about it, REX is the lesson learned for reuse — so combine them when a question "
            "spans both. " + _REMA_REX_LINK + " CAUTION on REMA's MWh figure: it is a "
            "human-entered ESTIMATE of an event's impact — signed (a gain is possible), "
            "frequently zero, and covering solar as well as wind. It is NOT validated energy "
            "loss. Report it as REMA's recorded estimate, attributed as such; never present it "
            "as the loss, never sum it, and never put it beside a wind_stops figure without "
            "labelling which is which."
        ),
    ),
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_rema_qualitative_context(args):
    return await _ask("rema_qualitative_context", args)


@tool(
    "genie_rex_qualitative_context",
    _describe(
        "rex_qualitative_context",
        fallback=(
            "Ask the REX (Return of Experience) Genie space about lessons learned — a knowledge "
            "base of ~1,000 structured lessons from past events on renewable assets (wind, "
            "solar, hydro, BESS). USE for: what went wrong or well on past projects or "
            "operations; lessons learned, corrective actions and best practices from fleet "
            "events; impact assessment of past events (CAPEX, OPEX, schedule delay, revenue "
            "loss, HSE, reputation); REX alerts or urgent lessons needing attention; event "
            "history by plant, country, technology, supplier, project phase or knowledge area; "
            "and whether a lesson is relevant at entity, country, GBU or portfolio-ui level."
        ),
        guardrails=(
            "Complements genie_rema_qualitative_context (REMA): the two cover the SAME plants "
            "from different angles — REMA is the operational problem, its tracked actions and "
            "its MWh impact estimate; REX is the knowledge kept for future reference — so "
            "combine them when a question spans both. " + _REMA_REX_LINK + " DO NOT use for "
            "real-time or operational data (production metrics, energy output, availability, "
            "alarms, SCADA) or for financial reporting. CRITICAL: any energy- or "
            "production-loss figure appearing in REX is narrative and INFORMATIVE ONLY — never "
            "a validated loss. Do not quote it as the loss, and never compute loss from it; "
            "validated energy loss comes exclusively from the wind_stops table."
        ),
    ),
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_rex_qualitative_context(args):
    return await _ask("rex_qualitative_context", args)


@tool(
    "genie_lorin_qualitative_context",
    _describe(
        "lorin_qualitative_context",
        fallback=(
            "Ask the Lorinlog Events Genie space about events logged against fleet assets — the "
            "event log itself: what is open or closed on an asset, how severe it is, its risk "
            "tier, the narrative of the incident, and when it returned to service. USE for "
            "'what is open on X', 'which assets have HIGH severity events', or an asset's event "
            "history. Covers ALL technologies, not just wind."
        ),
        guardrails=(
            "Complements genie_rema_qualitative_context (the initiatives and actions tracked "
            "against a problem) and genie_rex_qualitative_context (the lesson kept for reuse): "
            "Lorinlog is the log entry itself, so combine them when a question spans what "
            "happened, what is being done about it, and what was learned."
        ),
    ),
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_lorin_qualitative_context(args):
    return await _ask("lorin_qualitative_context", args)


@tool(
    "genie_anomaly_detection",
    _describe(
        "anomaly_detection",
        fallback=(
            "Ask the anomaly-detection Genie space whether the prediction-vs-observation "
            "algorithm flagged an anomaly, or for quantitative evidence of a component problem."
        ),
        guardrails="This is the only anomaly source.",
    ),
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_anomaly_detection(args):
    return await _ask("anomaly_detection", args)


@tool(
    "genie_alarm_stop_correlation",
    _describe(
        "alarm_stop_correlation",
        fallback=(
            "Ask the alarm/stop-correlation Genie space how turbines behaved (stops and alarms) "
            "around an event such as a grid event, maintenance, or weather."
        ),
    ),
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_alarm_stop_correlation(args):
    return await _ask("alarm_stop_correlation", args)


@tool(
    "genie_fleet_reporting",
    _describe(
        "fleet_reporting",
        fallback=(
            "Ask the 'Datasquare Fleet perf KPIs' Genie space for official wind-fleet reporting "
            "KPIs at site/unit level — net generation, planned/unplanned unavailability (MWh & "
            "hours), curtailment, installed capacity, wind index — including BUDGET-vs-ACTUAL "
            "and forecast (plans & Net-Zero trajectories, via the `version` dimension), sliced "
            "by country, business unit, technology, or period. Use for reporting roll-ups, "
            "site-level KPI analysis, budget & previsions, or when the other spaces/tables "
            "have nothing."
        ),
    ),
    _GENIE_SCHEMA,
    annotations=_READ_ONLY,
)
async def genie_fleet_reporting(args):
    return await _ask("fleet_reporting", args)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

GENIE_AGENT_TOOLS = [
    genie_rema_qualitative_context,
    genie_rex_qualitative_context,
    genie_lorin_qualitative_context,
    genie_anomaly_detection,
    genie_alarm_stop_correlation,
    genie_fleet_reporting,
]
# See the note in genie_tools.py: a name here that does not match a @tool name above
# makes the tool silently unavailable rather than raising.
GENIE_AGENT_TOOL_NAMES = [
    "mcp__databricks__genie_rema_qualitative_context",
    "mcp__databricks__genie_rex_qualitative_context",
    "mcp__databricks__genie_lorin_qualitative_context",
    "mcp__databricks__genie_anomaly_detection",
    "mcp__databricks__genie_alarm_stop_correlation",
    "mcp__databricks__genie_fleet_reporting",
]
