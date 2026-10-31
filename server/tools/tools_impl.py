"""Read-only Databricks tools for the Turbine Lens_MVP loop (claude-agent-sdk).

Two tools, exposed as an in-process SDK MCP server:
  * execute_sql      — SELECT/WITH only (gated in code AND by UC grants)
  * get_table_schema — comment + DDL (with column comments) + stats

The heavy lifting reuses the vendored databricks_tools_core.sql functions.
Sync core calls run in a thread so they don't block the loop's event loop.
Auth resolves via get_workspace_client() → OAuth M2M (the app's SP) from env.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..config import SQL_WAREHOUSE_ID, ALLOWED_TABLES, GENIE_SPACES
from ..databricks_tools_core.sql import (
    TableStatLevel,
    execute_sql as _execute_sql,
    get_table_stats_and_schema as _get_table_stats_and_schema,
)


def _is_select_only(sql: str) -> bool:
    """True only if every statement is a SELECT or WITH (read-only)."""
    s = re.sub(r"--.*?$", "", sql, flags=re.M)  # strip line comments
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)  # strip block comments
    stmts = [x.strip() for x in s.split(";") if x.strip()]
    if not stmts:
        return False
    for st in stmts:
        head = st.lstrip("( \t\r\n").split(None, 1)[0].lower() if st else ""
        if head not in ("select", "with"):
            return False
    return True


def _format_markdown(rows: list[dict[str, Any]]) -> str:
    """Render SQL rows as a compact markdown table (cheaper tokens than JSON)."""
    if not rows:
        return "(no results)"
    cols = list(rows[0].keys())
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        cells = ["" if r.get(c) is None else str(r.get(c)).replace("|", "\\|") for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


@tool(
    "execute_sql",
    "Run a READ-ONLY SQL query (SELECT/WITH only) on the Databricks SQL warehouse. "
    "Data spans multiple catalogs — ALWAYS use fully-qualified catalog.schema.table names. "
    "Returns a markdown table. Use this to cross-check Genie results or explore allowed tables.",
    {"sql_query": str},
)
async def execute_sql(args: dict[str, Any]) -> dict[str, Any]:
    sql = (args.get("sql_query") or "").strip()
    if not _is_select_only(sql):
        return {
            "content": [{"type": "text", "text": "Error: only read-only SELECT/WITH queries are allowed."}],
            "is_error": True,
        }
    try:
        rows = await asyncio.to_thread(_execute_sql, sql_query=sql, warehouse_id=SQL_WAREHOUSE_ID, timeout=120)
        return {"content": [{"type": "text", "text": _format_markdown(rows)}]}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"Error ({type(e).__name__}): {e}"}], "is_error": True}


@tool(
    "get_table_schema",
    "Get the schema of a Unity Catalog table: table COMMENT, full DDL (with per-column comments), "
    "column types and row stats. Use fully-qualified parts. Call this to understand columns before querying — "
    "column meanings come from the UC comments/DDL, do not guess.",
    {"catalog": str, "schema": str, "table": str},
)
async def get_table_schema(args: dict[str, Any]) -> dict[str, Any]:
    try:
        # NONE = schema + comment + DDL, no stats scan. Fast, and sufficient to
        # understand a table. Row counts (if needed) come from execute_sql COUNT.
        res = await asyncio.to_thread(
            _get_table_stats_and_schema,
            catalog=args["catalog"],
            schema=args["schema"],
            table_names=[args["table"]],
            table_stat_level=TableStatLevel.NONE,
            warehouse_id=SQL_WAREHOUSE_ID,
        )
        payload = res.model_dump(exclude_none=True) if hasattr(res, "model_dump") else res
        return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"Error ({type(e).__name__}): {e}"}], "is_error": True}


# --- Interactive charts (safe: no shell, no agent-run code) -------------------
# render_chart takes a Plotly figure SPEC (already-shaped data) and writes it to
# a JSON file, returning a ```plotly fence for the answer. The app reads the file
# and renders it inline (see app._render_plotly_blocks). Same interactive charts
# as agent-written plotting code, but WITHOUT granting the agent Bash/Edit — the
# read-only posture is preserved.
_CHART_JSON_DIR = os.environ.get("TURBINE_LENS_CHART_DIR", "/tmp/turbine_lens_charts")
_MAX_FIGURE_BYTES = 2_000_000  # cap a single figure spec at ~2 MB


@tool(
    "render_chart",
    "Render an interactive Plotly chart INLINE in the chat. Pass `figure`: a Plotly "
    "figure spec as a JSON object with a 'data' key (list of trace dicts, e.g. "
    "{'type':'bar','x':[...],'y':[...]}) and an optional 'layout'. IMPORTANT: shape and "
    "aggregate the data with execute_sql/Genie FIRST, then pass the final values here — "
    "do not compute large arrays yourself. Returns a ```plotly code fence; copy it VERBATIM "
    "into your answer exactly where the chart should appear. Use for any chart / graph / "
    "plot / visualization request.",
    {"figure": dict},
)
async def render_chart(args: dict[str, Any]) -> dict[str, Any]:
    fig = args.get("figure")
    if isinstance(fig, str):  # some models pass a JSON string instead of an object
        try:
            fig = json.loads(fig)
        except Exception as e:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"Error: `figure` is not valid JSON ({e})."}],
                "is_error": True,
            }
    if not isinstance(fig, dict) or not isinstance(fig.get("data"), list):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: `figure` must be a JSON object with a 'data' key (a list of Plotly traces).",
                }
            ],
            "is_error": True,
        }
    try:
        blob = json.dumps(fig)
    except Exception as e:  # noqa: BLE001
        return {
            "content": [{"type": "text", "text": f"Error: `figure` is not JSON-serialisable ({e})."}],
            "is_error": True,
        }
    if len(blob) > _MAX_FIGURE_BYTES:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error: figure too large ({len(blob)} bytes). Aggregate in SQL to fewer points.",
                }
            ],
            "is_error": True,
        }
    try:
        os.makedirs(_CHART_JSON_DIR, exist_ok=True)
        path = os.path.join(_CHART_JSON_DIR, f"{uuid.uuid4().hex[:12]}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(blob)
    except Exception as e:  # noqa: BLE001
        return {
            "content": [{"type": "text", "text": f"Error: could not save chart ({type(e).__name__}: {e})."}],
            "is_error": True,
        }
    fence = f"```plotly\n{path}\n```"
    return {
        "content": [
            {
                "type": "text",
                "text": "Chart saved. Include this fence VERBATIM in your answer, on its own lines, where the "
                "chart should appear:\n\n" + fence,
            }
        ]
    }


@tool(
    "render_echarts",
    "Render a premium interactive Apache ECharts chart INLINE in the chat (smooth animation "
    "+ rich interaction — PREFER this for charts). Pass `option`: an ECharts option spec as a "
    "JSON object, e.g. {'xAxis':{'type':'category','data':[...]}, 'yAxis':{'type':'value'}, "
    "'series':[{'type':'bar','data':[...]}]}. Shape/aggregate the data with execute_sql/Genie "
    "FIRST — pass only the final values. For a tall or dashboard-style layout add a top-level "
    "'_height' (pixels). Returns a ```echarts code fence; copy it VERBATIM into your answer "
    "exactly where the chart should appear. Use for any chart / graph / plot / visualization.",
    {"option": dict},
)
async def render_echarts(args: dict[str, Any]) -> dict[str, Any]:
    opt = args.get("option")
    if isinstance(opt, str):  # some models pass a JSON string instead of an object
        try:
            opt = json.loads(opt)
        except Exception as e:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"Error: `option` is not valid JSON ({e})."}],
                "is_error": True,
            }
    if not isinstance(opt, dict) or not (opt.get("series") or opt.get("dataset")):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: `option` must be a JSON object with a 'series' (list of ECharts "
                    "series) — e.g. {'series':[{'type':'bar','data':[...]}]}.",
                }
            ],
            "is_error": True,
        }
    try:
        blob = json.dumps(opt)
    except Exception as e:  # noqa: BLE001
        return {
            "content": [{"type": "text", "text": f"Error: `option` is not JSON-serialisable ({e})."}],
            "is_error": True,
        }
    if len(blob) > _MAX_FIGURE_BYTES:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error: option too large ({len(blob)} bytes). Aggregate in SQL to fewer points.",
                }
            ],
            "is_error": True,
        }
    try:
        os.makedirs(_CHART_JSON_DIR, exist_ok=True)
        path = os.path.join(_CHART_JSON_DIR, f"ec_{uuid.uuid4().hex[:12]}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(blob)
    except Exception as e:  # noqa: BLE001
        return {
            "content": [{"type": "text", "text": f"Error: could not save chart ({type(e).__name__}: {e})."}],
            "is_error": True,
        }
    fence = f"```echarts\n{path}\n```"
    return {
        "content": [
            {
                "type": "text",
                "text": "Chart saved. Include this fence VERBATIM in your answer, on its own lines, where the "
                "chart should appear:\n\n" + fence,
            }
        ]
    }


def get_tool_server():
    """Return (mcp_server, allowed_tool_names) for the read-only tool set:
    read-data tools + render_echarts/render_chart + the four Genie evidence tools."""
    from .genie_tools import GENIE_TOOL_NAMES, GENIE_TOOLS

    server = create_sdk_mcp_server(
        name="databricks",
        tools=[execute_sql, get_table_schema, render_echarts, render_chart, *GENIE_TOOLS],
    )
    allowed = [
        "mcp__databricks__execute_sql",
        "mcp__databricks__get_table_schema",
        "mcp__databricks__render_echarts",
        "mcp__databricks__render_chart",
        *GENIE_TOOL_NAMES,
    ]
    return server, allowed
