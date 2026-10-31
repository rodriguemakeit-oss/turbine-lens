"""Session title generation (heuristic + async LLM upgrade)."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Optional

from ..config import MODEL

logger = logging.getLogger(__name__)

_TITLE_FILLER_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|hey[,\s]+|hi[,\s]+|"
    r"i\s+(?:want|need|would\s+like)\s+(?:to\s+)?(?:know\s+)?|show\s+me\s+|give\s+me\s+|"
    r"tell\s+me\s+|what\s+(?:is|are|was|were)\s+(?:the\s+)?|list\s+|find\s+|get\s+me\s+)+",
    re.IGNORECASE,
)


def heuristic_title(question: str, maxlen: int = 48) -> str:
    """Instant title from the first question."""
    q = " ".join((question or "").split())
    stripped = _TITLE_FILLER_RE.sub("", q).strip(" ?.!,:;-")
    q = stripped or q or "New session"
    if len(q) > maxlen:
        q = (q[:maxlen].rsplit(" ", 1)[0] or q[:maxlen]) + "\u2026"
    return (q[:1].upper() + q[1:]) if q else "New session"


def llm_title(question: str, answer: str, host: str, token: str) -> Optional[str]:
    """One-shot FMAPI call -> a 3-6 word topic title."""
    prompt = (
        "Generate a concise 3-6 word title (Title Case, no quotes, no trailing "
        "punctuation) summarizing the TOPIC of this fleet-analytics conversation.\n\n"
        f"User: {(question or '')[:600]}\n\nAssistant: {(answer or '')[:800]}\n\nTitle:"
    )
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 24,
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
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        txt = "".join(p.get("text", "") for p in (data.get("content") or []) if isinstance(p, dict))
        txt = " ".join(txt.split()).strip().strip('"').strip("'").rstrip(".!?,").strip()
        return txt[:60] or None
    except Exception:
        logger.warning("LLM title generation failed", exc_info=True)
        return None


async def generate_title_async(
    session_id: str,
    question: str,
    answer: str,
    host: str,
    token: str,
    expected_title: str | None,
) -> None:
    """Background task: upgrade session title from heuristic to LLM-generated.

    Runs **once** per session, after its first turn — a session title is the user's to
    own from then on, and renaming is a UI feature. Two things enforce that, because
    either alone leaves a hole:

    * The caller only spawns this for a genuine first turn (see `routers/agent.py`).
      "First turn" was derived from a database read that can fail, and on failure every
      later turn looks like the first one.
    * `expected_title` makes the write conditional on the title still being the one we
      are upgrading. A user who renames the session while this first answer streams
      would otherwise have their name silently replaced seconds later.
    """
    import asyncio

    try:
        title = await asyncio.to_thread(llm_title, question, answer, host, token)
        if title:
            # Lazy import to avoid sqlalchemy at module level
            from ..db.database import set_title_if_unchanged

            applied = await asyncio.to_thread(
                set_title_if_unchanged, session_id, expected_title, title
            )
            if not applied:
                # Not an error — the expected outcome when the owner renamed it. Logged
                # because a title that silently refuses to change is otherwise a mystery.
                logger.info(
                    "auto-title for session %s skipped: title is no longer %r "
                    "(renamed by its owner, or already titled)",
                    session_id,
                    expected_title,
                )
    except Exception:
        logger.warning("generate_title_async failed", exc_info=True)
