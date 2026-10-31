"""Generate a session report: read the session, ask the model once, store a version.

One entry point (`generate`) behind two triggers — a chat request through the
`generate_report` MCP tool, and the sidebar button. Both go through here so the document's
structure is a server contract rather than something the agent has to remember.

Not an agent turn: a single FMAPI call in the shape of `title_generator.llm_title`, with no
tools and no loop. The model writes prose only. Everything checkable — which turns count,
which Genie spaces and tables were used, what the charts plotted, whether a figure was
actually said — is computed in `report_facts` first and either handed to the model as
grounding or rendered without asking it at all.

Reads nothing but the session's own rows. No Genie call, no warehouse query: a report must
not be able to contradict the conversation it summarises, so it is explicitly a snapshot and
says so in the document.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from ..config import MODEL
from ..db import database as db
from . import chart_store, history, report_facts, report_renderer

logger = logging.getLogger(__name__)

#: A whole session at once — far more than a turn's replay budget, because the value of a
#: report is in covering everything. ~50k chars ≈ 12k tokens, one call.
TRANSCRIPT_CHAR_CAP = 50_000
PER_MESSAGE_CAP = 12_000
_TIMEOUT_SECONDS = 120


class ReportError(Exception):
    """Generation failed in a way the user must be told about.

    Unlike a chart or a trace, a report is the thing the user asked for — failing silently
    would leave them waiting for a document that will never appear.
    """


def _prompt(transcript: str, evidence: dict, charts: list[dict], title: str) -> str:
    return f"""You are writing a fleet-analysis report for an operations manager, a fleet
expert and a fleet supervisor. They did not run the analysis and will not read any SQL.

Below is a Turbine Lens session: a read-only fleet analyst answering questions about wind
assets. Write a structured report about what it established.

RULES
- Only use what the session below actually says. If something was not established, say so
  rather than filling space.
- NEVER write a number that does not appear in the session. Copy figures exactly.
- No SQL, no query text, no table names in your prose, no mention of tools or prompts.
- Plain operational language. No preamble, no meta-commentary about being an AI.
- Return ONLY a JSON object with exactly these keys, no markdown fence, no commentary.

FIELDS
{report_renderer.prompt_fields()}

GROUNDING (computed from what the session recorded — trust this over your own reading)
{json.dumps({"evidence": evidence, "charts": charts}, ensure_ascii=False)[:8000]}

SESSION TITLE: {title}

SESSION TRANSCRIPT
{transcript}
"""


def _call_model(prompt: str, host: str, token: str) -> dict:
    """One FMAPI call returning the parsed JSON object.

    Retried once: a single malformed response is common enough to be worth another attempt,
    and cheap compared with telling the user their report failed.
    """
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/serving-endpoints/anthropic/v1/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as r:
        data = json.load(r)
    text = "".join(p.get("text", "") for p in (data.get("content") or []) if isinstance(p, dict)).strip()
    # Models like to wrap JSON in a fence even when told not to.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in the response (got {text[:120]!r})")
    return json.loads(text[start : end + 1])


def _coerce(raw: dict) -> dict:
    """Keep the declared fields, drop anything else, and fill what is missing.

    The renderer must never receive a shape it did not ask for; a missing section renders as
    "not applicable", which is honest, whereas a stray key would be silently lost.
    """
    payload = report_renderer.empty_payload()
    for key in payload:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(payload[key], list):
            payload[key] = value if isinstance(value, list) else [value]
        else:
            payload[key] = str(value)
    return payload


def generate(session_id: str, created_by: str | None, host: str, token: str) -> dict:
    """Produce and store one report version. Returns {id, version, created_at, flagged}.

    Synchronous and blocking on purpose: both callers (an MCP tool inside a turn, and an
    HTTP POST) want the finished report's id to hand back, and it takes seconds.
    """
    if not db.reports_available():
        raise ReportError("Report storage is unavailable, so a report cannot be saved.")

    messages = db.get_session_messages(session_id)
    meta = db.get_session_meta(session_id) or {}
    usable = report_facts.select_messages(messages)
    evidence = report_facts.evidence_facts(usable)

    # "Has an answer in it" is not the same as "has analysis in it": a session whose only
    # answer is the agent declining to write a report would pass that test and produce a
    # document of empty sections. Since the agent never states a figure without a tool behind
    # it, at least one tool call is the honest floor — and it makes the button refuse exactly
    # what the chat path already refuses.
    did_work = evidence["genie_total"] or evidence["sql_reads"] or evidence["schema_reads"] or evidence["charts"]
    if not any((m.get("role") or "") == "assistant" for m in usable) or not did_work:
        raise ReportError("This session has no analysis to report on yet — ask a data question first.")

    transcript, _ = history.render_transcript(
        usable,
        budget=TRANSCRIPT_CHAR_CAP,
        keep_chart_fences=True,
        per_message_cap=PER_MESSAGE_CAP,
    )

    # Chart specs are the one structured numeric evidence a report can have: result rows are
    # deliberately not persisted, but a chart carries the values that were drawn.
    names = [n for m in usable for n in chart_store.names_in(m.get("content") or "")]
    specs = {}
    for name in names:
        spec = db.get_chart_spec(name) if db.charts_available() else None
        if spec:
            specs[name] = spec
    charts = report_facts.chart_facts(usable, specs, chart_store.names_in)

    prompt = _prompt(transcript, evidence, charts, meta.get("title") or "")
    last_error: Exception | None = None
    payload = None
    for attempt in (1, 2):
        try:
            payload = _coerce(_call_model(prompt, host, token))
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("report generation attempt %d failed: %s", attempt, exc)
    if payload is None:
        raise ReportError(f"The report could not be generated ({last_error}).") from last_error

    # A figure nobody said is the one failure a summariser must not be trusted on. Flagged,
    # not blocked: the report is still worth having, but a flagged one deserves a human read.
    flagged = report_facts.check_figures(
        report_renderer.factual_text(payload),
        transcript,
        allowed={str(v) for v in evidence.values() if isinstance(v, int)},
        # Chart values and their top-k running totals count as supported: they are grounding
        # we handed the model on purpose, and summing them is what a report is meant to do.
        known_values=report_facts.chart_numbers(charts),
    )
    if flagged:
        logger.warning(
            "report for session %s has %d figure(s) with no traceable origin in the session "
            "text, its charts, or a top-k total of them: %s",
            session_id,
            len(flagged),
            flagged[:10],
        )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    version_meta = {
        "session_id": session_id,
        "session_title": meta.get("title"),
        "owner_name": meta.get("owner_name"),
        "owner_email": meta.get("owner_email"),
        "generated_at": generated_at,
        "turns_covered": evidence.get("turns", 0),
        "trace_ids": [m["trace_id"] for m in usable if m.get("trace_id")],
    }

    # The HTML is frozen at insert time — that is what makes a version immutable when the
    # template later improves. Rendering happens inside the transaction because the document
    # prints its own version number.
    row = db.create_report(
        session_id,
        payload,
        len(messages),
        created_by,
        render_html=lambda version: report_renderer.render(
            payload,
            {**version_meta, "version": version},
            evidence,
            charts,
            specs,
        ),
    )
    logger.info(
        "report %s v%d for session %s: %d chars transcript, %d chart(s), %d flagged figure(s)",
        row["id"],
        row["version"],
        session_id,
        len(transcript),
        len(charts),
        len(flagged),
    )
    return {**row, "flagged_figures": flagged}
