"""Conversation replay — the agent's memory comes from Lakebase, not from disk.

`resume=<cli_session_id>` reopened a transcript **file** under
`~/.claude/projects/<escaped-cwd>/`, and Databricks Apps containers are ephemeral: after
any deploy or restart that file is gone, the CLI exits 1, and the user gets no answer at
all. So a continued turn no longer resumes anything — it starts a fresh CLI session and
replays the prior conversation out of our own `messages` rows, which is how ChatGPT and
Claude.ai work and why their continuity survives anything.

What can be replayed is exactly what we persist per message: the answer text and
`steps_json`. Tool *results* are deliberately not stored (the mirrored `tool_result`
event carries only an id; full outputs go to MLflow traces), so the agent is told to
re-query when it needs the underlying rows rather than left to assume it still has them.

Stdlib only, on purpose: there is no local Python runtime for this app, and a
dependency-free module can be exercised with `python3 -c` before it is ever deployed.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

#: Total characters of conversation replayed into a turn. Characters, not tokens: an
#: honest proxy that needs no tokenizer. ~30k chars ≈ 7.5k tokens.
DEFAULT_CHAR_BUDGET = 30_000
#: Per-message cap, so one enormous answer cannot crowd out the rest of the history.
MAX_MESSAGE_CHARS = 6_000
MAX_STEPS_PER_MESSAGE = 3
MAX_STEP_CHARS = 160
#: Room for the "[N earlier messages omitted]" marker, reserved before filling the tail
#: so adding the marker cannot push the result back over budget.
_MARKER_RESERVE = 96

#: Same pattern the client renders with (ChatMessage.tsx); group 1 is the chart's path.
#: Replaying a fence invites the agent to re-emit another turn's chart verbatim, so in
#: replay it becomes a placeholder. Defined in this module because it has no dependencies
#: and can therefore be imported from anywhere — services/chart_store.py reuses it for
#: the delete cascade rather than keeping a second copy in sync.
CHART_FENCE_RE = re.compile(r"```(?:plotly|echarts)\s*\n(.*?)\n```", re.S)

#: Step labels that describe the turn's bookkeeping rather than its evidence.
_NON_EVIDENCE_PREFIXES = ("💰", "🧠")

_OPEN = "<conversation_so_far>"
_CLOSE = "</conversation_so_far>"
_BRIDGE = (
    "The block above is our earlier conversation in this session, provided for "
    "continuity. Tool results from those turns are not available — re-run a query if "
    "you need the underlying numbers. Answer the current question below."
)


def _configured_budget() -> int:
    raw = os.environ.get("TURBINE_LENS_HISTORY_CHAR_BUDGET")
    if not raw:
        return DEFAULT_CHAR_BUDGET
    try:
        value = int(raw)
    except ValueError:
        logger.warning("TURBINE_LENS_HISTORY_CHAR_BUDGET=%r is not an int — using default", raw)
        return DEFAULT_CHAR_BUDGET
    return value if value > 0 else DEFAULT_CHAR_BUDGET


def _evidence(steps) -> str:
    """One compact line naming the tools a past answer actually used.

    The labels carry the real SQL string and the real Genie question (see `_step_label`
    in routers/agent.py), which is what stops the agent re-running work it has already
    done — the closest thing to tool-result context we can offer from what we store.
    """
    if not isinstance(steps, list):
        return ""
    labels = []
    for step in steps:
        if not isinstance(step, str):
            continue
        text = " ".join(step.split())
        if not text or text.startswith(_NON_EVIDENCE_PREFIXES):
            continue
        if len(text) > MAX_STEP_CHARS:
            text = text[:MAX_STEP_CHARS].rstrip() + "…"
        labels.append(text)
        if len(labels) >= MAX_STEPS_PER_MESSAGE:
            break
    return "\n  (evidence: " + " | ".join(labels) + ")" if labels else ""


def _render(
    msg: dict,
    *,
    unanswered: bool = False,
    keep_chart_fences: bool = False,
    per_message_cap: int = MAX_MESSAGE_CHARS,
) -> str:
    is_user = (msg.get("role") or "").lower() == "user"
    speaker = "User" if is_user else "Turbine Lens"
    if unanswered:
        speaker += " (interrupted — never answered)"

    content = msg.get("content") or ""
    if not keep_chart_fences:
        content = CHART_FENCE_RE.sub("[chart]", content)
    content = content.strip()
    if len(content) > per_message_cap:
        content = content[:per_message_cap].rstrip() + " …[truncated]"

    rendered = f"{speaker}: {content}".rstrip()
    if not is_user:
        rendered += _evidence(msg.get("steps_json"))
    return rendered


def render_transcript(
    messages: list[dict] | None,
    *,
    budget: int | None = None,
    keep_chart_fences: bool = False,
    per_message_cap: int = MAX_MESSAGE_CHARS,
) -> tuple[str, dict]:
    """Turn message rows into a labelled transcript. Returns (block, stats).

    Shared by two callers with different needs, which is why the knobs exist rather than a
    second serializer: **replay** budgets the text and replaces chart fences with a
    placeholder, while the **report generator** wants the whole conversation and keeps the
    fences (they are how it knows which chart belonged where). `budget=None` means no
    limit — callers that want the configured replay budget resolve it themselves.
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict) and (m.get("content") or "").strip()]
    stats = {
        "history_messages": 0,
        "history_total": len(msgs),
        "history_omitted": 0,
        "history_chars": 0,
        "history_truncated": False,
    }
    if not msgs:
        return "", stats

    # No user/assistant pairing: a turn killed by a restart persists its question and
    # never its answer (the write is in a `finally` the dead process never reached), so
    # pairing would mis-align the whole transcript. Walking the list and labelling the
    # gap is both simpler and honest about what happened.
    rendered = []
    for i, msg in enumerate(msgs):
        following = msgs[i + 1] if i + 1 < len(msgs) else None
        unanswered = (msg.get("role") or "").lower() == "user" and (
            following is None or (following.get("role") or "").lower() == "user"
        )
        rendered.append(
            _render(
                msg,
                unanswered=unanswered,
                keep_chart_fences=keep_chart_fences,
                per_message_cap=per_message_cap,
            )
        )

    limit = budget
    join_cost = 2  # the "\n\n" between entries
    total = sum(len(e) + join_cost for e in rendered)

    if limit is None or total <= limit:
        kept, omitted = rendered, 0
    else:
        # The first user message is the scope anchor ("for the Villeveyrac farm, June
        # 2026") and is worth more than any middle turn, so it is kept unconditionally —
        # which also means one message is the floor, even under an absurdly small budget.
        anchor, rest = rendered[0], rendered[1:]
        used = len(anchor) + join_cost + _MARKER_RESERVE
        tail: list[str] = []
        for entry in reversed(rest):
            cost = len(entry) + join_cost
            if used + cost > limit:
                break
            tail.append(entry)
            used += cost
        tail.reverse()
        omitted = len(rest) - len(tail)
        marker = f"[{omitted} earlier message(s) omitted to fit the context budget]"
        kept = [anchor, marker, *tail] if omitted else [anchor, *tail]

    block = "\n\n".join(kept)
    stats.update(
        history_messages=len(kept) - (1 if omitted else 0),
        history_omitted=omitted,
        history_chars=len(block),
        history_truncated=bool(omitted),
    )
    return block, stats


def build_agent_input(
    message: str,
    prior_messages: list[dict] | None,
    *,
    budget: int | None = None,
) -> tuple[str, dict]:
    """Return (what to send the agent, stats about the replay).

    `prior_messages` is what `db.get_session_messages` returns, and must NOT include the
    current turn's user message. With no history the message is returned **unchanged**,
    so a session's first turn behaves exactly as it did before replay existed.
    """
    block, stats = render_transcript(
        prior_messages,
        budget=budget if budget is not None else _configured_budget(),
    )
    if not block:
        return message, stats

    agent_input = f"{_OPEN}\n{block}\n{_CLOSE}\n\n{_BRIDGE}\n\n<current_question>\n{message}\n</current_question>"
    return agent_input, stats


def truncation_note(stats: dict) -> str | None:
    """A step label for the UI when history was dropped, else None.

    Surfaced as a step rather than a banner because steps are already persisted with the
    answer (`steps_json`), so the note survives a reload. Quiet, but never absent: a
    user who cannot see that older context was dropped will believe the agent still
    remembers it.
    """
    if not stats.get("history_truncated"):
        return None
    # Not "the last N": what survives is the first message plus the newest that fit, so
    # naming a position would be wrong. Only the counts are honest.
    return (
        f"🧠 Context: replayed {stats['history_messages']} of "
        f"{stats['history_total']} earlier messages — {stats['history_omitted']} omitted "
        "to fit the context budget"
    )
