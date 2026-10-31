"""Facts a session report is built from — computed, never recalled.

The report generator asks the model to write prose, but everything checkable is derived
here in code first: which turns count, which Genie spaces and tables were actually used,
what the charts actually plotted, and whether a figure in the draft really appears in the
session. A report that *claims* provenance is worth much less than one that has it.

Stdlib only, deliberately: there is no local Python runtime for this app, and a
dependency-free module can be exercised with `python3` before it is ever deployed — the
same discipline as services/history.py.
"""

from __future__ import annotations

import re

#: Fence the report tool asks the agent to echo, mirroring CHART_FENCE_RE in history.py.
#: Group 1 is the report id.
REPORT_FENCE_RE = re.compile(r"```report\s*\n(.*?)\n```", re.S)

# --- Step-label parsing ----------------------------------------------------
# ⚠️ These patterns read the *display* strings produced by `_step_label` in
# routers/agent.py. They key on the stable words ("Genie ·", "SQL:", "Inspected schema:")
# rather than the emoji, but a wording change there still breaks these counts silently —
# so the two belong changed together.
_GENIE_RE = re.compile(r"Genie\s*·\s*([^:]+):")
_SQL_RE = re.compile(r"\bSQL:\s*`(.*)`", re.S)
_SCHEMA_RE = re.compile(r"Inspected schema:\s*`([^`]+)`")
_SKILL_RE = re.compile(r"Skill loaded:\s*\*\*([^*]+)\*\*")
#: catalog.schema.table, as the allowlist requires them to be written.
_FQN_RE = re.compile(r"\b([a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+)\b", re.I)


def report_ids_in(text: str) -> list[str]:
    """Report ids referenced by an answer's fences."""
    if not text:
        return []
    return [raw.strip() for raw in REPORT_FENCE_RE.findall(text) if raw.strip()]


def _role(msg: dict) -> str:
    return (msg.get("role") or "").lower()


def select_messages(messages: list[dict] | None) -> list[dict]:
    """The turns a report should summarise.

    Two kinds of turn are dropped, both for the same reason — they are *about* reporting
    rather than about the fleet:

      * an answer that carries a report fence, together with the request that produced it.
        Without this, the second report's summary opens with "the user asked for a report".
      * a trailing user message with no answer. On the prompt path that is the very turn
        asking for this report (its answer does not exist yet); otherwise it is a question
        killed by a restart. Neither contributes anything to summarise.
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    skip: set[int] = set()
    for i, msg in enumerate(msgs):
        if _role(msg) == "assistant" and report_ids_in(msg.get("content") or ""):
            skip.add(i)
            if i > 0 and _role(msgs[i - 1]) == "user":
                skip.add(i - 1)
    if msgs and _role(msgs[-1]) == "user":
        skip.add(len(msgs) - 1)
    return [m for i, m in enumerate(msgs) if i not in skip]


def evidence_facts(messages: list[dict]) -> dict:
    """What the session actually did, for the approach/sources section.

    Counts, spaces and tables — never the query text. The audience is an ops manager; a
    reader who wants the SQL goes back to the session or the trace.
    """
    genie: dict[str, int] = {}
    tables: dict[str, int] = {}
    skills: set[str] = set()
    sql_reads = schema_reads = charts = 0

    for msg in messages:
        for label in msg.get("steps_json") or []:
            if not isinstance(label, str):
                continue
            text = " ".join(label.split())

            m = _GENIE_RE.search(text)
            if m:
                space = m.group(1).strip()
                genie[space] = genie.get(space, 0) + 1
                continue

            m = _SQL_RE.search(text)
            if m:
                sql_reads += 1
                for fqn in _FQN_RE.findall(m.group(1)):
                    tables[fqn.lower()] = tables.get(fqn.lower(), 0) + 1
                continue

            m = _SCHEMA_RE.search(text)
            if m:
                schema_reads += 1
                tables.setdefault(m.group(1).lower(), 0)
                continue

            m = _SKILL_RE.search(text)
            if m:
                skills.add(m.group(1).strip())
                continue

            if "render_echarts" in text or "render_chart" in text:
                charts += 1

    return {
        "genie_calls": dict(sorted(genie.items(), key=lambda kv: -kv[1])),
        "genie_total": sum(genie.values()),
        "sql_reads": sql_reads,
        "schema_reads": schema_reads,
        "tables": sorted(tables),
        "skills": sorted(skills),
        "charts": charts,
        "turns": sum(1 for m in messages if _role(m) == "user"),
    }


# --- Chart facts -----------------------------------------------------------
#: How many values per series are quoted to the model. A *sample*, not the series: enough to
#: state a figure, short enough that a dashboard-sized chart cannot dominate the prompt.
#: Whenever it truncates, the true point count and total go alongside — a sample presented as
#: if it were everything makes the model miscount, and it did: a 177-point map produced "a
#: total of 24 French wind farms were mapped".
MAX_SERIES = 6
MAX_VALUES = 24


def _title_of(spec: dict) -> str | None:
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


def _all_categories(spec: dict) -> list:
    axis = spec.get("xAxis")
    if isinstance(axis, list) and axis:
        axis = axis[0]
    if isinstance(axis, dict) and isinstance(axis.get("data"), list):
        return axis["data"]
    return []


def _categories_of(spec: dict) -> list:
    return _all_categories(spec)[:MAX_VALUES]


def _magnitudes(values: list) -> list:
    """The quantity each data point represents, dropping positional coordinates.

    A geo point is `[lon, lat, size]` and a scatter point `[x, y]`, where only the last
    element is the figure a reader cares about. Handing the model the whole triple made it
    echo longitudes into a management report — so the coordinates never leave here.
    """
    out = []
    for v in values:
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, list):
            v = next((x for x in reversed(v) if isinstance(x, (int, float))), None)
        out.append(v)
    return out


def chart_facts(messages: list[dict], specs: dict[str, dict], chart_names) -> list[dict]:
    """The numbers each chart actually plotted.

    This is the closest thing to tool-result evidence the report can have: result rows are
    deliberately not persisted (only an id is mirrored; full outputs go to MLflow), but a
    chart spec carries the values that were drawn. `chart_names` is injected so this stays
    dependency-free — the caller supplies the fence scanner from history.py.
    """
    facts = []
    for msg in messages:
        if _role(msg) == "user":
            continue
        for name in chart_names(msg.get("content") or ""):
            spec = specs.get(name)
            if not isinstance(spec, dict):
                continue
            series = []
            for s in (spec.get("series") or spec.get("data") or [])[:MAX_SERIES]:
                if not isinstance(s, dict):
                    continue
                raw = s.get("data")
                if raw is None:
                    raw = s.get("y")
                if not isinstance(raw, list):
                    series.append({"name": s.get("name"), "type": s.get("type"), "values": None})
                    continue
                # Kept in the plotted order so a value still lines up with its category —
                # sorting by size would let the model pair the wrong name with the wrong figure.
                magnitudes = _magnitudes(raw)
                numeric = [v for v in magnitudes if isinstance(v, (int, float))]
                entry = {
                    "name": s.get("name"),
                    "type": s.get("type"),
                    "point_count": len(magnitudes),
                    "values_total": round(sum(numeric), 3) if numeric else None,
                    "values": magnitudes[:MAX_VALUES],
                }
                if len(magnitudes) > MAX_VALUES:
                    entry["values_note"] = (
                        f"only the first {MAX_VALUES} of {len(magnitudes)} points are listed; "
                        "use point_count and values_total for anything about the whole series"
                    )
                series.append(entry)
            categories = _categories_of(spec)
            facts.append(
                {
                    "name": name,
                    "title": _title_of(spec),
                    "categories": categories,
                    "category_count": len(_all_categories(spec)),
                    "series": series,
                }
            )
    return facts


# --- Figure check ----------------------------------------------------------
#: Standalone numbers only: a leading letter, underscore or hyphen means this is an
#: identifier (E04, A-06, WIF_FREPN, T3), not a figure.
#:
#: The first alternative is space-grouped thousands — "194 280 MWh" is one number, and the
#: French and Spanish sessions in this app write figures that way. It requires a 1-3 digit
#: lead group so that "in 2026 280 turbines" stays two numbers rather than becoming 2026280.
_NUMBER_RE = re.compile(r"(?<![\w.,\-])(?:\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d[\d.,]*)(?![\w])")
#: Below this, a number is structural noise (list positions, "3 sites") rather than a
#: finding worth verifying.
_MIN_DIGITS = 2


def _normalise(token: str) -> float | None:
    """Parse a written figure, tolerating 1,234.5 / 1.234,5 / 1 234,5 grouping."""
    raw = token.strip().rstrip(".,")
    if not raw:
        return None
    raw = raw.replace(" ", " ").replace(" ", " ")
    if " " in raw:  # space-grouped thousands, optionally with a decimal part
        head, _, tail = raw.rpartition(",") if "," in raw.split(" ")[-1] else (raw, "", "")
        if head and tail:
            return _normalise(head.replace(" ", "") + "," + tail)
        return _normalise(raw.replace(" ", ""))
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):  # pure thousands grouping
        return float(re.sub(r"[.,]", "", raw))
    # The last separator is the decimal point, whichever character it is.
    cut = max(raw.rfind("."), raw.rfind(","))
    if cut == -1:
        try:
            return float(raw)
        except ValueError:
            return None
    head, tail = re.sub(r"[.,]", "", raw[:cut]), raw[cut + 1 :]
    try:
        return float(f"{head}.{tail}") if tail else float(head)
    except ValueError:
        return None


def _figures(text: str) -> list[tuple[str, float, int]]:
    out = []
    for token in _NUMBER_RE.findall(text or ""):
        token = token.strip().rstrip(".,")
        digits = sum(c.isdigit() for c in token)
        if digits < _MIN_DIGITS:
            continue
        value = _normalise(token)
        if value is None:
            continue
        cut = max(token.rfind("."), token.rfind(","))
        decimals = len(token) - cut - 1 if cut != -1 and len(token) - cut - 1 <= 4 else 0
        out.append((token, value, decimals))
    return out


def _rounding_step(token: str, value: float) -> float:
    """How precisely a written figure claims to be stated.

    "190 000" has four trailing zeros, so it asserts a value to the nearest 10 000 — and
    "roughly 190 000" for a true 194 280 is a report doing its job, not inventing. Reading the
    precision off the number itself keeps the tolerance honest: "58181" claims exactness and
    gets no slack at all.
    """
    digits = re.sub(r"\D", "", token)
    if not digits or value != int(value):
        return 1.0
    trailing = len(digits) - len(digits.rstrip("0"))
    # Never allow more slack than the figure's own magnitude justifies.
    return float(10 ** min(trailing, max(0, len(digits) - 2)))


def chart_numbers(charts: list[dict]) -> set[float]:
    """Every figure a chart legitimately supports.

    The plotted values themselves, plus the running totals of each series' largest values.
    Those totals exist because "the top five farms account for 194 280 MWh" is the single
    most common quantified claim in a report, and it is *arithmetic on evidence we supplied*,
    not an invention — without this the figure check flags the best sentence in the document.
    Anything beyond a top-k sum stays flagged, which is the honest outcome: it means a human
    should confirm the number rather than that the model made it up.
    """
    out: set[float] = set()
    for chart in charts or []:
        if isinstance(chart.get("category_count"), int):
            out.add(float(chart["category_count"]))
        for series in chart.get("series") or []:
            # The counts and totals we hand over are evidence too: "177 farms" and the
            # series total are things the model is *supposed* to be able to state.
            for key in ("point_count", "values_total"):
                if isinstance(series.get(key), (int, float)):
                    out.add(float(series[key]))
            values = []
            for v in _magnitudes(series.get("values") or []):
                if isinstance(v, (int, float)):
                    values.append(float(v))
                    out.add(float(v))
            running = 0.0
            for v in sorted(values, reverse=True)[:12]:
                running += v
                out.add(round(running, 6))
    return out


def check_figures(
    report_text: str, session_text: str, allowed: set[str] | None = None, known_values: set[float] | None = None
) -> list[str]:
    """Figures in the draft that the session does not support. Empty is the good case.

    "Supported" means: said in the conversation, plotted in one of its charts, or a running
    total of a chart's largest values. A rounded quote counts too — a report saying 412 for a
    session's 412.37 is doing its job.

    What survives is a number with no traceable origin. That is the one failure mode a
    summariser must not be trusted on, but it can also be legitimate arithmetic we cannot
    reproduce, so this flags for review rather than blocking: the report is still worth
    having, and a flagged one deserves a human read.
    """
    known = [v for _, v, _ in _figures(session_text)]
    known.extend(known_values or set())
    extra = {t for t in (allowed or set())}
    flagged = []
    for token, value, decimals in _figures(report_text):
        if token in extra:
            continue
        if any(round(k, decimals) == round(value, decimals) for k in known):
            continue
        step = _rounding_step(token, value)
        if step > 1 and any(round(k / step) * step == value for k in known):
            continue
        flagged.append(token)
    return flagged
