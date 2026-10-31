"""Read-only Databricks tools for the Turbine Lens_MVP loop (claude-agent-sdk).

Exposed as an in-process SDK MCP server:
  * execute_sql      — SELECT/WITH only (gated in code AND by UC grants)
  * get_table_schema — comment + DDL (with column comments) + stats
  * resolve_site     — canonical wind site/farm, or the candidate list to ask the user about

The heavy lifting reuses the vendored databricks_tools_core.sql functions.
Sync core calls run in a thread so they don't block the loop's event loop.
Auth resolves via get_workspace_client() → OAuth M2M (the app's SP) from env.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool


from ..config import READ_CATALOG, SQL_WAREHOUSE_ID
from ..databricks_tools_core.sql import TableStatLevel
from ..databricks_tools_core.sql import execute_sql as _execute_sql
from ..databricks_tools_core.sql import get_table_stats_and_schema as _get_table_stats_and_schema
from ..services import chart_store
from mcp.types import ToolAnnotations

_READ_ONLY = ToolAnnotations(readOnlyHint=True)


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
    annotations=_READ_ONLY,
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
    annotations=_READ_ONLY,
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


# --- Site resolution (the human check on an ambiguous site name) --------------
# Users asked to be shown the candidates whenever Turbine Lens is unsure which site they
# mean, instead of it announcing "the closest match" and moving on. That instruction
# already existed in `turbine_lens-verification` and the loop overrode it, so the check is
# enforced here by *withholding the identifier*: an ambiguous resolve returns labels
# and nothing else, so there is no `farm_code` to pass to a Genie tool until the user
# has chosen. `site_resolver` carries the matching rationale.
#
# Note the query below takes no parameters at all. That is deliberate — user text is
# never interpolated into SQL, so the apostrophe-escaping trap that makes
# `ILIKE '%L''Epine%'` match nothing on Databricks cannot reach this path.
# Tier 1 — the wind reference. `status` comes along because the inferred `active` flag
# cannot see it: the registry holds 25 DECOMMISSIONED farms that the presence rule calls
# active, and the caller has to be able to say so.
#
# The catalog is interpolated from config (READ_CATALOG), which the bundle sets
# per environment. That is a deployment-time constant, not caller input — the
# apostrophe-escaping note above is about the site name the user typed, and that
# still never reaches SQL.
_FARM_DIM_SQL = f"""
SELECT farm,
       farm_code,
       count(*) AS turbines,
       concat_ws(' / ', collect_set(manufacturer)) AS manufacturer,
       concat_ws(' / ', collect_set(model)) AS model,
       concat_ws(' / ', collect_set(status)) AS registry_status
FROM {READ_CATALOG}.pic.wind_turbine
GROUP BY farm, farm_code
"""

# Tier 2 — every farm of every technology, including those with no turbines recorded.
# Grain in the table is one row per substation, so it is rolled up to one row per farm.
_HIERARCHY_DIM_SQL = f"""
SELECT farm,
       farm_code,
       max(farm_technology) AS farm_technology,
       max(technology) AS technology,
       max(business_unit) AS business_unit,
       count(DISTINCT substation_code) AS substations
FROM {READ_CATALOG}.pic.hierarchy
GROUP BY farm, farm_code
"""

# Both are slow-moving reference data (~559 and ~1484 rows) and a turn may resolve
# several sites, so they are cached per process (single instance — invariant #7).
_FARM_CACHE_TTL_SECONDS = 900
_farm_cache: dict[str, Any] = {"wind": None, "hierarchy": None, "at": 0.0}

_SITE_SCOPE_NOTE = (
    f"Two tiers: {READ_CATALOG}.pic.wind_turbine is the wind reference and is "
    f"checked first; {READ_CATALOG}.pic.hierarchy is the fallback and covers ALL "
    "technologies (wind, solar, BESS, hybrid) and all farms, connected or not."
)

# The agreed rule is presence-based, and it is wrong in two knowable ways. Whoever
# reports `active` has to report these with it — it is inferred, not measured.
_ACTIVE_CAVEATS = [
    "`active` is INFERRED from presence in pic.wind_turbine, not read from a column — "
    "no table in scope records grid-connection state (raccorde / non raccorde / en "
    "cours de raccordement).",
    "The wind registry contains 25 DECOMMISSIONED farms, which this rule reports as "
    "active. Check `registry_status` and say DECOMMISSIONED when it says so.",
    "There is no non-wind registry, so every solar / BESS / hybrid farm resolves from "
    "the hierarchy and is reported not active whatever its true state. For those, say "
    "the connection state is not known rather than asserting it is inactive.",
]


async def _load_farms() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(wind registry rows, fleet hierarchy rows), cached together so they never skew."""
    now = time.monotonic()
    if _farm_cache["wind"] is not None and (now - _farm_cache["at"]) < _FARM_CACHE_TTL_SECONDS:
        return _farm_cache["wind"], _farm_cache["hierarchy"]
    wind, hierarchy = await asyncio.gather(
        asyncio.to_thread(
            _execute_sql, sql_query=_FARM_DIM_SQL, warehouse_id=SQL_WAREHOUSE_ID, timeout=120
        ),
        asyncio.to_thread(
            _execute_sql, sql_query=_HIERARCHY_DIM_SQL, warehouse_id=SQL_WAREHOUSE_ID, timeout=120
        ),
    )
    _farm_cache["wind"] = wind
    _farm_cache["hierarchy"] = hierarchy
    _farm_cache["at"] = now
    return wind, hierarchy


_RESOLVE_SITE_DESCRIPTION = (
    "Resolve a user-supplied site / farm name to the canonical value the data uses, and "
    "say whether the farm is ACTIVE. Checks pic.wind_turbine (the wind reference) first, "
    "then falls back to pic.hierarchy, which covers ALL technologies (wind, solar, BESS, "
    "hybrid) and every farm including those with no turbines recorded. "
    "CALL THIS FIRST, before any Genie tool or SQL filtered to a site — the farm name and "
    "`farm_code` it returns are the ONLY site values you may pass on. Matching is "
    "accent- and apostrophe-insensitive and matches names as well as codes, so "
    "\"l'epine\", \"L’Épine Amanty\" and \"WIF_FREPN\" all work. "
    "ALWAYS tell the user whether the resolved farm is active or not active, and pass on "
    "the `active_caveats` — `active` is inferred from presence in the wind registry, not "
    "read from a column, so it is wrong for a DECOMMISSIONED farm (see `registry_status`) "
    "and uninformative for non-wind farms. "
    "If the name is ambiguous or unknown the result carries `needs_user_choice: true` and "
    "NO identifier: STOP, show the user the `candidates` labels verbatim, ask which site "
    "they mean, and end your turn. Do not pick the closest one, do not resolve the site "
    "yourself with execute_sql, and do not query any source in the meantime. When the user "
    "answers, call this again with their `choice` number (or the full name they gave) to get "
    "the identifier. Turbine names are NOT resolved here — the hierarchy has no turbine "
    "level, so a turbine lookup stays a pic.wind_turbine query."
)


@tool(
    "resolve_site",
    _RESOLVE_SITE_DESCRIPTION,
    {"name": str, "choice": int},
    annotations=_READ_ONLY,
)
async def resolve_site(args: dict[str, Any]) -> dict[str, Any]:
    from . import site_resolver

    name = (args.get("name") or "").strip()
    if not name:
        return {"content": [{"type": "text", "text": "Error: 'name' is required."}], "is_error": True}
    try:
        choice = int(args.get("choice") or 0)
    except (TypeError, ValueError):
        choice = 0

    try:
        wind_rows, hierarchy_rows = await _load_farms()
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"Error ({type(e).__name__}): {e}"}], "is_error": True}

    found = site_resolver.resolve(name, wind_rows, hierarchy_rows)
    status = found["status"]
    candidates = found["candidates"]
    tier = found["tier"]

    def _identity(c: dict) -> dict:
        active = bool(c.get("active"))
        registry_status = c.get("registry_status")
        payload = {
            "status": "resolved",
            "farm": c.get("farm"),
            "farm_code": c.get("farm_code"),
            "active": active,
            "active_label": "active" if active else "not active",
            "resolved_from": c.get("source"),
            "matched_on": c.get("match"),
            "scope": _SITE_SCOPE_NOTE,
            "active_caveats": _ACTIVE_CAVEATS,
            "note": (
                "State which site you resolved to AND whether it is active, then use "
                "`farm` / `farm_code` verbatim from here on."
            ),
        }
        if c.get("source") == site_resolver.WIND_REGISTRY:
            payload.update(
                {
                    "turbines": c.get("turbines"),
                    "manufacturer": c.get("manufacturer"),
                    "model": c.get("model"),
                    "registry_status": registry_status,
                }
            )
            if registry_status and registry_status != "IN OPERATION":
                payload["active_contradiction"] = (
                    f"The wind registry records this farm as {registry_status}, which "
                    "contradicts the inferred `active: true`. Report the registry value — "
                    "it is read from the data, `active` is not."
                )
        else:
            payload.update(
                {
                    "technology": c.get("farm_technology") or c.get("technology"),
                    "business_unit": c.get("business_unit"),
                    "substations": c.get("substations"),
                    "turbines": None,
                    "why_not_active": (
                        "This farm is absent from pic.wind_turbine, which is what the "
                        "presence rule reads as not active. If it is not a wind farm, say "
                        "its connection state is not known rather than that it is inactive."
                    ),
                }
            )
        if found.get("same_name_other_technologies"):
            payload["same_name_other_technologies"] = found["same_name_other_technologies"]
        return payload

    # The candidate order is derived from `name` alone, so re-running the match
    # reproduces the same list and `choice` stays meaningful without server state.
    if choice and candidates:
        if status in ("exact", "unique"):
            payload = _identity(candidates[0])
        elif 1 <= choice <= len(candidates):
            payload = _identity(candidates[choice - 1])
        else:
            payload = {
                "status": "bad_choice",
                "needs_user_choice": True,
                "query": name,
                "message": f"`choice` must be between 1 and {len(candidates)} for this name.",
                "candidates": [
                    {"choice": i, "label": site_resolver.label(c)} for i, c in enumerate(candidates, 1)
                ],
            }
        return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}

    if status in ("exact", "unique"):
        payload = _identity(candidates[0])
    elif status == "none":
        # Both tiers missed. That is now a much stronger statement than it was when the
        # lookup was wind-only — the hierarchy holds every technology — but hydro still
        # lives in its own table, so absence is still not proof.
        all_farms = sorted({str(f.get("farm")) for f in hierarchy_rows})
        sample = all_farms[:20]
        payload = {
            "status": "not_found",
            "needs_user_choice": True,
            "query": name,
            "scope": _SITE_SCOPE_NOTE,
            "searched": [site_resolver.WIND_REGISTRY, site_resolver.FLEET_HIERARCHY],
            "example_sites_note": (
                f"An alphabetical sample of {len(sample)} of {len(all_farms)} farms across all "
                "technologies, to show the naming style. NOT the full list — never count or "
                "reason over it."
            ),
            "directive": (
                "STOP and ask the user. Tell them no farm matches this name in either the wind "
                "registry or the fleet hierarchy, and ask them to confirm the exact name or give "
                "the farm code. Hydro sites live in a separate hierarchy that this lookup does "
                "not read, so do NOT state flatly that the site does not exist, and do NOT query "
                "any source until they answer."
            ),
            "example_sites": sample,
        }
    else:
        payload = {
            "status": "ambiguous",
            "needs_user_choice": True,
            "query": name,
            "scope": _SITE_SCOPE_NOTE,
            "match_count": found["total"],
            "truncated": found["truncated"],
            "resolved_from": tier,
            "active_caveats": _ACTIVE_CAVEATS,
            "directive": (
                "STOP and ask the user which site they mean. List every candidate label below, "
                "verbatim and in this order — each says whether that farm is active, which the "
                "user needs to see — and end your turn there. No identifier is returned while "
                "the name is ambiguous, so you cannot query a site yet — do NOT pick the "
                "closest match, do NOT resolve it yourself with execute_sql, and do NOT call a "
                "Genie tool. Call resolve_site again with `choice` once they answer."
            ),
            "candidates": [
                {
                    "choice": i,
                    "label": site_resolver.label(c),
                    "match": c["match"],
                    "active": bool(c.get("active")),
                }
                for i, c in enumerate(candidates, 1)
            ],
        }
        if found.get("same_name_other_technologies"):
            payload["same_name_other_technologies"] = found["same_name_other_technologies"]
        if found["truncated"]:
            payload["truncation_note"] = (
                f"{found['total']} sites matched; only the {len(candidates)} strongest are listed. "
                "Tell the user the list is partial and ask them to be more specific."
            )

    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


# --- Interactive charts (safe: no shell, no agent-run code) -------------------
# render_chart takes a Plotly figure SPEC (already-shaped data) and writes it to
# a JSON file, returning a ```plotly fence for the answer. The app reads the file
# and renders it inline (see app._render_plotly_blocks). Same interactive charts
# as agent-written plotting code, but WITHOUT granting the agent Bash/Edit — the
# read-only posture is preserved.
_CHART_JSON_DIR = os.environ.get("TURBINE_LENS_CHART_DIR", "/tmp/turbine_lens_charts")
_MAX_FIGURE_BYTES = 2_000_000  # cap a single figure spec at ~2 MB


async def _save_chart(spec: dict, blob: str, prefix: str) -> str:
    """Write a chart spec to disk, mirror it to Lakebase, return the file path.

    Both render tools share this so they cannot drift apart. The file is what the
    renderer reads; the Lakebase copy is what makes the chart outlive this container —
    `TURBINE_LENS_CHART_DIR` is on the ephemeral disk, so before the mirror existed every
    chart in an older conversation 404'd after a restart. The mirror is best-effort
    inside chart_store, so a database problem costs durability and nothing else.
    """
    os.makedirs(_CHART_JSON_DIR, exist_ok=True)
    name = f"{prefix}{uuid.uuid4().hex[:12]}.json"
    path = os.path.join(_CHART_JSON_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(blob)
    await chart_store.save(name, spec)
    return path


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
        path = await _save_chart(fig, blob, "")
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
        path = await _save_chart(opt, blob, "ec_")
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


# --- Session report -----------------------------------------------------------
# A pure trigger: the agent passes NO content. The server reads the session's own rows,
# fills a fixed field list and renders a fixed template, so the document has the same parts
# every time instead of depending on the agent remembering them. That is also why there is
# no skill for this — a skill would be a second description of a layout only the template
# can enforce (CLAUDE.md invariant #6).
_REPORT_TOOL_DESCRIPTION = (
    "Generate the session's PDF-ready report and return a link to it. Call this when the "
    "user asks for a report, a summary document, a PDF, or something to share with "
    "management. Takes no arguments — the server reads this session itself and writes every "
    "section, so do NOT try to pass content or write the report yourself. "
    "Do NOT call it on a session that has no analysis yet: say there is nothing to report on "
    "and offer to look something up first. After it succeeds, reply with ONE short sentence "
    "plus the returned fence VERBATIM — do not restate the report's contents in the chat."
)


def _make_generate_report(session_id: str):
    """Build the report tool bound to one session.

    The session id is closed over at construction rather than read from a context variable,
    because the tool server is already built per turn (see `_run_agent_background`), which
    makes this the cheapest place to know which conversation we are in.
    """

    @tool("generate_report", _REPORT_TOOL_DESCRIPTION, {})
    async def generate_report(args: dict[str, Any]) -> dict[str, Any]:
        from ..services import report_generator
        from ..services.user import get_fmapi_credentials

        try:
            host, token = get_fmapi_credentials()
            result = await asyncio.to_thread(report_generator.generate, session_id, None, host, token)
        except report_generator.ReportError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        except Exception as e:  # noqa: BLE001
            return {
                "content": [
                    {"type": "text", "text": f"Error: the report could not be generated ({type(e).__name__})."}
                ],
                "is_error": True,
            }

        fence = f"```report\n{result['id']}\n```"
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Report v{result['version']} created. Include this fence VERBATIM in "
                    "your answer, on its own lines, and add at most one sentence of "
                    "context:\n\n" + fence,
                }
            ]
        }

    return generate_report


def get_tool_server(session_id: str | None = None, mode: str | None = None):
    """Return the MCP server and allowed tool names.

    The returned server always includes the read-only SQL/chart tools and then
    conditionally includes the Genie tools for the requested mode.
    If session_id is provided, the report tool is added for that conversation.
    """
    if not mode:
        from ..config import get_genie_mode

        mode = get_genie_mode()

    if mode == "agent":
        from .genie_agent_tools import GENIE_AGENT_TOOL_NAMES as genie_names
        from .genie_agent_tools import GENIE_AGENT_TOOLS as genie_tools
    else:
        from .genie_tools import GENIE_TOOL_NAMES as genie_names
        from .genie_tools import GENIE_TOOLS as genie_tools

    tools = [execute_sql, get_table_schema, resolve_site, render_echarts, render_chart, *genie_tools]
    allowed = [
        "mcp__databricks__execute_sql",
        "mcp__databricks__get_table_schema",
        "mcp__databricks__resolve_site",
        "mcp__databricks__render_echarts",
        "mcp__databricks__render_chart",
        *genie_names,
    ]

    if session_id:
        tools.append(_make_generate_report(session_id))
        allowed.append("mcp__databricks__generate_report")

    server = create_sdk_mcp_server(name="databricks", tools=tools)
    return server, allowed
