"""The session report document: its structure, and the HTML it renders to.

This module *is* the document's contract. The field list below is what the generator asks
the model to fill and what the template lays out, so there is exactly one description of
the structure — deliberately not a skill, which would be a second description of a shape
only this template can actually enforce (CLAUDE.md invariant #6).

Everything the model wrote is HTML-escaped on the way in. The report is server-rendered
markup, so escaping is a security boundary rather than tidiness: model output must never be
able to inject markup into a document that gets shared onward.

Imports no `db`, so the whole layout can be exercised locally with `python3`.
"""

from __future__ import annotations

import html
import json

from ..config import LOSS_CATALOG
from . import chart_render

#: What the model is asked to produce, field by field. The generator turns this into its
#: instructions; the renderer lays each one out. Change the document here, once.
FIELD_SPEC: dict[str, str] = {
    "period": (
        "The time window the analysis covers, exactly as the conversation states it "
        "(e.g. 'June 2026', '22 Jul - 5 Aug 2026'). Empty string if it was never stated."
    ),
    "goal": (
        "2-3 sentences, plain language: what the user set out to learn and why it matters "
        "operationally. No preamble, no restating the question verbatim."
    ),
    "findings": (
        "3 to 5 objects {text, figure, source}. `text` is one sentence a fleet manager can "
        "act on; `figure` is the number that supports it, copied EXACTLY as it appears in "
        "the conversation (empty string if the finding has no number); `source` names where "
        "it came from in plain words (e.g. 'validated stop-loss', 'fleet reporting'). "
        "NEVER state a number that does not appear in the conversation."
    ),
    "sites": (
        "Objects {name, summary} for each site that was genuinely a focus of the analysis. "
        "`summary` is 1-2 sentences. Return [] if no individual site was a focus — do not "
        "invent one to fill the section."
    ),
    "turbines": (
        "Objects {name, summary} for individual turbines that were examined (e.g. 'E04'). "
        "This is maintenance-level detail. Return [] if none were."
    ),
    "models": (
        "Objects {name, summary} for turbine models, technologies or manufacturers that "
        "were examined (e.g. 'Goldwind', 'Vestas V90'). This is fleet-strategy level. "
        "Return [] if none were."
    ),
    "limitations": (
        "3-5 strings: what this analysis could NOT establish, specific to this session — "
        "data gaps, short windows, correlation vs causation, entities not covered. Do not "
        "repeat generic disclaimers about AI."
    ),
    "checks": (
        "2-4 strings: concrete verifications a human should perform before acting, each "
        "naming who or what to check (e.g. 'confirm the E04 generator alarm against the "
        "site's maintenance log')."
    ),
    "actions": (
        "2-4 objects {text, urgency} where urgency is exactly 'high', 'medium' or 'low'. "
        "Each is a concrete next step. Base them only on what the conversation established."
    ),
}

# --- Text the model may never write or reword -------------------------------
#: CLAUDE.md invariant #4. A loss figure travelling inside a forwarded document without
#: this definition will be read as total loss, which it is not.
LOSS_DEFINITION = (
    "Energy-loss figures in this report come from the validated stop-loss view "
    f"(<code>{LOSS_CATALOG}.telemetry.wind_stops</code>): a stop is counted only when "
    "it was both logged in the stop logbook <em>and</em> validated by algorithm. A stop that "
    "is absent from it means “not validated as a real loss” — which is not the "
    "same as “no loss occurred” — and it is a different measure from the "
    "unavailability KPIs reported by the Genie spaces."
)
#: Why the numbers may not match today's data.
SNAPSHOT_CAVEAT = (
    "This report summarises what the session established at the time it was generated. It "
    "was <strong>not</strong> re-verified against current data, and no new queries were run "
    "to produce it. Figures may have moved since."
)
#: Section 6 is the assistant's judgement, and readers may act on it.
ACTIONS_CAVEAT = (
    "Suggested by Turbine Lens from this session only, and not independently validated. Treat "
    "as a starting point for a human decision."
)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _para(text) -> str:
    """Escaped text with blank lines kept as paragraphs."""
    chunks = [c.strip() for c in str(text or "").split("\n\n") if c.strip()]
    return "\n".join(f"<p>{_esc(c)}</p>" for c in chunks) or "<p class='muted'>—</p>"


def _na(what: str) -> str:
    return f"<p class='muted'>Not applicable — no individual {what} was a focus of this session.</p>"


def _entities(items, what: str) -> str:
    rows = [i for i in (items or []) if isinstance(i, dict) and (i.get("name") or "").strip()]
    if not rows:
        return _na(what)
    out = []
    for item in rows:
        out.append(f"<div class='entity'><h4>{_esc(item.get('name'))}</h4><p>{_esc(item.get('summary'))}</p></div>")
    return "\n".join(out)


def _bullets(items) -> str:
    rows = [str(i).strip() for i in (items or []) if str(i or "").strip()]
    if not rows:
        return "<p class='muted'>—</p>"
    return "<ul>" + "".join(f"<li>{_esc(r)}</li>" for r in rows) + "</ul>"


def _findings(items) -> str:
    rows = [i for i in (items or []) if isinstance(i, dict) and (i.get("text") or "").strip()]
    if not rows:
        return "<p class='muted'>No quantified finding was recorded for this session.</p>"
    out = []
    for f in rows:
        figure = (f.get("figure") or "").strip()
        source = (f.get("source") or "").strip()
        badge = f"<span class='figure'>{_esc(figure)}</span>" if figure else ""
        src = f"<span class='src'>{_esc(source)}</span>" if source else ""
        out.append(f"<li>{badge}<span class='ftext'>{_esc(f.get('text'))}</span>{src}</li>")
    return "<ol class='findings'>" + "".join(out) + "</ol>"


def _actions(items) -> str:
    rows = [i for i in (items or []) if isinstance(i, dict) and (i.get("text") or "").strip()]
    if not rows:
        return "<p class='muted'>—</p>"
    out = []
    for a in rows:
        urgency = str(a.get("urgency") or "").lower()
        urgency = urgency if urgency in ("high", "medium", "low") else "medium"
        out.append(
            f"<li><span class='urgency u-{urgency}'>{_esc(urgency)}</span><span>{_esc(a.get('text'))}</span></li>"
        )
    return "<ul class='actions'>" + "".join(out) + "</ul>"


def _sources_prose(evidence: dict) -> str:
    """Section 4, written from what was recorded — never from the model's recall."""
    bits = []
    genie = evidence.get("genie_calls") or {}
    if genie:
        spaces = ", ".join(
            f"{_esc(space.replace('_', ' '))} ({n} question{'s' if n != 1 else ''})" for space, n in genie.items()
        )
        bits.append(f"Genie was asked {evidence.get('genie_total', 0)} question(s): {spaces}.")
    reads = evidence.get("sql_reads") or 0
    if reads:
        bits.append(f"{reads} direct read(s) of the governed tables were run to cross-check.")
    if evidence.get("schema_reads"):
        bits.append(f"{evidence['schema_reads']} table description(s) were inspected first.")
    tables = evidence.get("tables") or []
    if tables:
        names = ", ".join(f"<code>{_esc(t)}</code>" for t in tables)
        bits.append(f"Tables involved: {names}.")
    if evidence.get("charts"):
        bits.append(f"{evidence['charts']} chart(s) were produced.")
    if not bits:
        bits.append("No data tool was used in this session; it was conversational only.")
    return "<p>" + " ".join(bits) + "</p>"


def _figures(charts: list[dict], specs: dict) -> str:
    if not charts:
        return ""
    blocks = []
    for i, chart in enumerate(charts):
        name = chart.get("name") or ""
        spec = specs.get(name)
        if not isinstance(spec, dict):
            continue
        title = chart.get("title") or f"Figure {i + 1}"
        blocks.append(
            f"<figure class='chart'><figcaption>{_esc(title)}</figcaption>"
            # SVG so it prints crisp rather than at screen resolution.
            + chart_render.block_html(f"fmchart{i}", spec, name=name, renderer="svg")
            + "</figure>"
        )
    if not blocks:
        return ""
    return "<h3>Figures from this session</h3>" + "\n".join(blocks)


_CSS = """
:root { --ink:#1F2937; --muted:#6B7280; --line:#E5E7EB; --brand:#1ABFA6; }
* { box-sizing: border-box; }
body { margin:0; font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
       color: var(--ink); background:#F9FAFB; line-height:1.55; }
.page { max-width: 820px; margin: 0 auto; background:#fff; padding: 32px 40px 56px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing:.06em; color: var(--brand);
     margin: 30px 0 8px; padding-bottom:6px; border-bottom:2px solid var(--line); }
h3 { font-size: 14px; margin: 20px 0 8px; }
h4 { font-size: 13px; margin: 0 0 2px; }
p, li { font-size: 13.5px; }
.sub { color: var(--muted); font-size: 12.5px; margin:0; }
.muted { color: var(--muted); font-style: italic; }
table.scope { width:100%; border-collapse:collapse; margin-top:16px; font-size:12.5px; }
table.scope th, table.scope td { text-align:left; padding:6px 10px; border:1px solid var(--line);
     vertical-align: top; }
table.scope th { width:170px; background:#F9FAFB; font-weight:600; color:var(--muted); }
ol.findings { list-style:none; padding:0; margin:0; }
ol.findings li { border-left:3px solid var(--brand); padding:8px 12px; margin-bottom:8px;
     background:#F9FEFD; }
.figure { display:inline-block; font-weight:700; margin-right:8px; }
.src { display:block; color:var(--muted); font-size:11.5px; margin-top:3px; }
.entity { border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin-bottom:8px; }
ul.actions { list-style:none; padding:0; }
ul.actions li { display:flex; gap:10px; align-items:flex-start; margin-bottom:7px; }
.urgency { flex:0 0 62px; text-align:center; font-size:10.5px; text-transform:uppercase;
     border-radius:4px; padding:2px 4px; font-weight:700; letter-spacing:.04em; }
.u-high { background:#FEE2E2; color:#B91C1C; }
.u-medium { background:#FEF3C7; color:#92400E; }
.u-low { background:#E5E7EB; color:#374151; }
.note { border:1px solid var(--line); border-left:3px solid #9CA3AF; background:#F9FAFB;
     padding:10px 12px; font-size:12.5px; margin:10px 0; }
figure.chart { margin:0 0 18px; border:1px solid var(--line); border-radius:6px; padding:8px; }
figure.chart figcaption { font-size:12.5px; font-weight:600; margin-bottom:4px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:11.5px;
     background:#F3F4F6; padding:1px 4px; border-radius:3px; }
footer { margin-top:34px; padding-top:12px; border-top:1px solid var(--line);
     color:var(--muted); font-size:11.5px; }
.bar { position:sticky; top:0; background:#111827; color:#fff; padding:8px 40px;
     display:flex; justify-content:space-between; align-items:center; font-size:12.5px; }
.bar button { background:var(--brand); color:#fff; border:0; border-radius:6px;
     padding:7px 14px; font-size:12.5px; font-weight:600; cursor:pointer; }
@media print {
  /* Browser-supplied headers and footers are a print-dialog setting we cannot fully
     override; the margin here at least keeps them off the content. */
  @page { margin: 15mm; }
  body { background:#fff; }
  .page { max-width:none; padding:0; }
  .no-print { display:none !important; }
  h2 { break-after: avoid; }
  figure.chart, .entity, ol.findings li, .note { break-inside: avoid; }
}
"""


def render(payload: dict, meta: dict, evidence: dict, charts: list[dict], chart_specs: dict) -> str:
    """The frozen HTML for one report version.

    Stored as-is, so what a recipient reads cannot change when the template later improves —
    a new version is generated instead.
    """
    payload = payload if isinstance(payload, dict) else {}
    echarts_needed = any(
        chart_render.is_echarts(c.get("name") or "", chart_specs.get(c.get("name") or "") or {}) for c in charts
    )
    plotly_needed = any(
        not chart_render.is_echarts(c.get("name") or "", chart_specs.get(c.get("name") or "") or {}) for c in charts
    )
    deps = (
        chart_render.deps_html(
            echarts=echarts_needed,
            plotly=plotly_needed,
            animate=False,
        )
        if charts
        else ""
    )

    period = (payload.get("period") or "").strip() or "not stated in the session"
    scope_rows = [
        (
            "Session",
            f"{_esc(meta.get('session_title') or 'Untitled')} "
            f"<span class='muted'>({_esc(meta.get('session_id'))})</span>",
        ),
        ("Analysis period", _esc(period)),
        ("Run by", _esc(meta.get("owner_name") or meta.get("owner_email") or "—")),
        ("Report version", f"v{_esc(meta.get('version'))} · generated {_esc(meta.get('generated_at'))}"),
        ("Covers", f"{_esc(meta.get('turns_covered'))} exchange(s) of this session"),
    ]
    scope = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in scope_rows)

    trace_ids = meta.get("trace_ids") or []
    traces = ", ".join(f"<code>{_esc(t)}</code>" for t in trace_ids[:12]) or "—"

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Turbine Lens report — {_esc(meta.get("session_title") or "session")}</title>
<style>{_CSS}</style>
{deps}
</head><body>
<div class="bar no-print">
  <span>Turbine Lens session report · v{_esc(meta.get("version"))}</span>
  <button onclick="window.print()">Save as PDF / Print</button>
</div>
<div class="page">
  <h1>Fleet analysis report</h1>
  <p class="sub">{_esc(meta.get("session_title") or "Untitled session")}</p>
  <table class="scope">{scope}</table>

  <h2>1 · Purpose &amp; summary</h2>
  {_para(payload.get("goal"))}
  <h3>Key findings</h3>
  {_findings(payload.get("findings"))}
  {_figures(charts, chart_specs)}

  <h2>2 · Sites in focus</h2>
  {_entities(payload.get("sites"), "site")}

  <h2>3 · Turbines &amp; models in focus</h2>
  <h3>Individual turbines</h3>
  {_entities(payload.get("turbines"), "turbine")}
  <h3>Models &amp; technologies</h3>
  {_entities(payload.get("models"), "model or technology")}

  <h2>4 · Approach &amp; sources</h2>
  {_sources_prose(evidence)}
  <div class="note">{LOSS_DEFINITION}</div>

  <h2>5 · Limitations &amp; suggested checks</h2>
  <div class="note">{SNAPSHOT_CAVEAT}</div>
  <h3>What this analysis could not establish</h3>
  {_bullets(payload.get("limitations"))}
  <h3>Checks to perform before acting</h3>
  {_bullets(payload.get("checks"))}

  <h2>6 · Recommended actions</h2>
  <div class="note">{ACTIONS_CAVEAT}</div>
  {_actions(payload.get("actions"))}

  <footer>
    Generated by Turbine Lens from session <code>{_esc(meta.get("session_id"))}</code> ·
    read-only fleet analyst. The full conversation is in the app; the per-turn traces are
    in MLflow: {traces}.
  </footer>
</div>
</body></html>"""


def prompt_fields() -> str:
    """The field list as instructions, so the generator cannot drift from the template."""
    return "\n".join(f"- {key}: {desc}" for key, desc in FIELD_SPEC.items())


def empty_payload() -> dict:
    """Shape with everything blank — what a validation failure must never be stored as."""
    return {k: ([] if k not in ("period", "goal") else "") for k in FIELD_SPEC}


#: Fields that assert something about the fleet, and so must only contain figures the
#: session supports. `checks` and `actions` are deliberately excluded: they propose future
#: work ("extend the window to 12 months", "inspect within 48 hours"), where a number is part
#: of a suggestion rather than a claim about what was found. Checking them turned the flag
#: list into noise, which is worse than not checking — a warning nobody reads is not a
#: safeguard.
FACTUAL_FIELDS = ("period", "goal", "findings", "sites", "turbines", "models", "limitations")


def factual_text(payload: dict) -> str:
    """Only the claim-bearing fields, for the figure check."""
    return json.dumps({k: payload.get(k) for k in FACTUAL_FIELDS}, ensure_ascii=False)


def payload_text(payload: dict) -> str:
    """Every string the model wrote."""
    return json.dumps(payload, ensure_ascii=False)
