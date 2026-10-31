"""Agent invocation + SSE streaming endpoints.

Pattern: POST /invoke_agent -> returns execution_id immediately.
         POST /stream_progress/{execution_id} -> SSE event stream (50s windows).
         POST /stop_stream/{execution_id} -> cancel.
"""

import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import PHASE0, GATEWAY, MODEL
from ..db import database as db
from ..services import execution_store, history, turn_limiter
from ..services.active_stream import get_stream_manager
from ..services.authz import require_session_owner
from ..services.title_generator import generate_title_async, heuristic_title
from ..services.user import get_current_user, get_current_user_name, get_fmapi_credentials

logger = logging.getLogger(__name__)
router = APIRouter()

SSE_WINDOW_SECONDS = 50

# asyncio holds only a weak reference to a bare create_task() result, so a
# long-running background task can be garbage-collected mid-flight. Anything that
# must outlive the request that started it goes through _spawn().
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class _TurnCancelled(Exception):
    """Stop was pressed while the turn was still queued for a slot.

    Distinct from a failure: nothing ran, so it must not be reported as an error.
    """


def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


class InvokeAgentRequest(BaseModel):
    session_id: str | None = None
    message: str
    genie_mode: str | None = None  # "agent" or "classic"; persisted to session


class InvokeAgentResponse(BaseModel):
    execution_id: str
    session_id: str


def _effective_genie_mode(requested: str | None, is_new_session: bool) -> str:
    """Runtime mode policy.

    - New chat: default to agent mode.
    - Existing chat: if session mode is absent from DB, default to classic.
    """
    if requested in ("agent", "classic"):
        return requested
    return "agent" if is_new_session else "classic"


@router.post("/invoke_agent", response_model=InvokeAgentResponse)
async def invoke_agent(request: Request, body: InvokeAgentRequest):
    """Start the agent asynchronously, return execution_id for streaming."""
    user_email = get_current_user(request)
    user_name = get_current_user_name(request)

    # Read-only guard. A turn sent into someone else's session advances that
    # session's cli_session_id, mutating the owner's conversation state — so
    # refuse before minting credentials or spawning anything.
    if body.session_id:
        await require_session_owner(request, body.session_id)

    fmapi_host, fmapi_token = get_fmapi_credentials()

    # Resolve or create session
    session_id = body.session_id
    is_new_session = session_id is None
    heuristic = None
    if not session_id:
        session_id = str(uuid.uuid4())
        heuristic = title = heuristic_title(body.message)
        if db.is_configured():
            try:
                await asyncio.to_thread(
                    db.create_session,
                    session_id=session_id,
                    owner_email=user_email,
                    owner_name=user_name,
                    title=title,
                )
            except Exception:
                logger.exception("Failed to create session in DB")

    # Create active stream
    stream_mgr = get_stream_manager()
    stream = await stream_mgr.create_stream(
        session_id=session_id,
        user_email=user_email,
    )
    stream.add_event({"type": "session.created", "session_id": session_id})

    # Mirror the execution to Lakebase so a reloaded page can find this turn.
    await execution_store.create(stream)
    _spawn(execution_store.run_flush_loop(stream))

    # The conversation the agent gets handed back — the same rows the UI renders, read
    # BEFORE this turn's question is appended, so they are the prior conversation and not
    # a duplicate of what is being asked now. This replaces `resume=<cli_session_id>`,
    # which reopened a transcript file on the container disk and therefore died with the
    # container; see services/history.py.
    prior_messages: list[dict] = []
    turn_idx = 0
    # Whether turn_idx is actually the turn number or just what a failed read left
    # behind. `turn_idx == 0` is not by itself evidence of a first turn: the read below
    # can fail, and then turn 20 is indistinguishable from turn 1 — which is enough to
    # re-run the once-per-session auto-title over a name the user chose.
    history_read_ok = False
    if db.is_configured():
        try:
            prior_messages = await asyncio.to_thread(db.get_session_messages, session_id)
            turn_idx = len(prior_messages)
            history_read_ok = True
            await asyncio.to_thread(db.add_message, session_id, turn_idx, "user", body.message)
        except Exception:
            # Degrades to a context-less turn rather than no turn. Whatever was read
            # before the failure is still valid history, so it is kept.
            logger.exception("DB failed while loading history")
            turn_idx = len(prior_messages)

    logger.info(
        "[CONTEXT] invoke_agent: session=%s, turn=%d, prior_messages=%d",
        session_id,
        turn_idx,
        len(prior_messages),
    )

    # Resolve mode for this turn.
    effective_mode = _effective_genie_mode(body.genie_mode, is_new_session)
    if not is_new_session and body.genie_mode not in ("agent", "classic") and db.is_configured():
        try:
            session_mode = await asyncio.to_thread(db.get_session_genie_mode, session_id)
            if session_mode in ("agent", "classic"):
                effective_mode = session_mode
        except Exception:
            logger.exception("Failed to read session genie mode")

    # Persist mode so subsequent turns in this session are explicit.
    if db.is_configured():
        try:
            await asyncio.to_thread(db.set_session_genie_mode, session_id, effective_mode)
        except Exception:
            logger.exception("Failed to persist session genie mode")

    # The title this turn is allowed to auto-name, or None for "leave the title alone".
    # A first turn is the only one that may name a session; after that the name belongs
    # to its owner, who can rename it in the sidebar.
    auto_title_expect: str | None = None
    if is_new_session:
        auto_title_expect = heuristic  # the title we just wrote ourselves
    elif history_read_ok and turn_idx == 0 and db.is_configured():
        # An empty session created ahead of time (POST /sessions) still deserves a real
        # name. Reading the title it currently has is what the upgrade is conditional on.
        try:
            meta = await asyncio.to_thread(db.get_session_meta, session_id)
            auto_title_expect = (meta or {}).get("title")
        except Exception:
            logger.exception("Failed to read session title; skipping auto-title")

    # Run agent in background
    _spawn(
        _run_agent_background(
            stream=stream,
            session_id=session_id,
            message=body.message,
            turn_idx=turn_idx,
            auto_title_expect=auto_title_expect,
            genie_mode=effective_mode,
            user_email=user_email,
            fmapi_host=fmapi_host,
            fmapi_token=fmapi_token,
            prior_messages=prior_messages,
        )
    )

    return InvokeAgentResponse(execution_id=stream.execution_id, session_id=session_id)


class StreamProgressRequest(BaseModel):
    last_event_index: int = 0


@router.post("/stream_progress/{execution_id}")
async def stream_progress(execution_id: str, body: StreamProgressRequest = StreamProgressRequest()):
    """SSE stream of events from an active execution (50s window).

    On reconnection, pass last_event_index to skip already-processed events.
    """
    stream_mgr = get_stream_manager()
    stream = await stream_mgr.get_stream(execution_id)
    if not stream:
        raise HTTPException(status_code=404, detail="Execution not found")

    start_index = body.last_event_index

    async def event_generator():
        start = time.time()
        sent_count = start_index
        while (time.time() - start) < SSE_WINDOW_SECONDS:
            # Send any new events (starting from where client left off)
            while sent_count < len(stream.events):
                yield sse_event(stream.events[sent_count])
                sent_count += 1

            if stream.is_complete:
                yield sse_event({"type": "done"})
                return

            if stream.is_cancelled:
                yield sse_event({"type": "cancelled"})
                return

            await stream.wait_for_event(timeout=1.0)

        # Window expired — client should reconnect with last_event_index
        yield sse_event({"type": "window_expired", "events_sent": sent_count})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stop_stream/{execution_id}")
async def stop_stream(request: Request, execution_id: str):
    """Cancel an active execution — only the user who started it may do so."""
    stream_mgr = get_stream_manager()
    stream = await stream_mgr.get_stream(execution_id)
    if not stream:
        raise HTTPException(status_code=404, detail="Execution not found")
    caller = get_current_user(request)
    if stream.user_email and stream.user_email != caller:
        logger.warning(
            "refused stop of execution %s: caller=%s owner=%s",
            execution_id,
            caller,
            stream.user_email,
        )
        raise HTTPException(status_code=403, detail="This execution is not yours.")
    stream.cancel()
    return {"success": True}


@router.get("/sessions/{session_id}/active_execution")
async def active_execution(request: Request, session_id: str, after: int = 0):
    """Is a turn already running for this session?

    A page that reloaded has lost its execution_id, so without this it would show
    nothing while a turn continued server-side. Live in-process streams are the
    truth and are checked first; Lakebase answers for anything this process is no
    longer running. `after` is the same cursor the SSE stream uses.
    """
    live = await get_stream_manager().find_active_by_session(session_id)
    if live:
        return {
            "execution_id": live.execution_id,
            "status": "running",
            "source": "memory",
            "event_count": len(live.events),
            "events": live.events[after:],
        }

    if db.executions_available():
        row = await asyncio.to_thread(db.get_latest_execution, session_id)
        if row:
            # Single instance (CLAUDE.md #7): no live stream means nothing is
            # running this, so it did not survive. Report that rather than let the
            # client wait on a turn that will never emit again.
            status = "interrupted" if row["status"] == "running" else row["status"]
            # Replay only where the events are the sole record of the answer. For a
            # completed or cancelled turn the message row is authoritative, and
            # replaying would double-render it.
            events = []
            if status in ("interrupted", "error"):
                events = await asyncio.to_thread(db.get_execution_events, row["execution_id"], after)
            return {**row, "status": status, "source": "lakebase", "events": events}

    return {"execution_id": None, "status": None, "event_count": 0, "events": []}


async def _run_agent_background(
    stream,
    session_id: str,
    message: str,
    turn_idx: int,
    genie_mode: str | None,
    user_email: str,
    fmapi_host: str,
    fmapi_token: str,
    prior_messages: list[dict] | None = None,
    auto_title_expect: str | None = None,
) -> None:
    """Run the agent loop and push events to the stream."""
    from ..services.agent import stream_agent_response
    from ..system_prompt import get_system_prompt
    from ..tracing import extract_output, log_turn_trace

    # Captured from the SDK's result event. No longer used to resume anything — kept
    # because the CLI logs this id, so it is the handle for correlating a turn with
    # `databricks apps logs`. The agent's context comes from `prior_messages`.
    cli_session_id: str | None = None

    final_text = ""
    error_message: str | None = None
    queued_ms = 0.0  # bound by turn_slot below; needed in finally even if we never ran
    full_thinking = ""
    steps_labels: list[str] = []
    trace_steps: list[dict] = []
    pending_tools: dict = {}
    cost_meta: dict = {}

    # Load tools if not phase0
    mcp_servers: dict = {}
    allowed_tools: list[str] = []
    skills_dir: str | None = None

    if not PHASE0:
        try:
            from ..tools import get_tool_server

            session_genie_mode = genie_mode
            # Bound to this session so `generate_report` knows what to summarise. The tool
            # server is built per turn, which is why no ContextVar is needed.
            tool_server, tool_names = get_tool_server(session_id=session_id, mode=session_genie_mode)
            mcp_servers = {"databricks": tool_server} if tool_server else {}
            allowed_tools = tool_names
            skills_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "skills"))
        except Exception:
            logger.exception("Failed to load MCP tools")

    system_prompt = get_system_prompt(phase0=PHASE0)

    # Replay the prior conversation into this turn. `message` must stay pristine: it is
    # what gets traced and what was persisted as the user's row, so tracing or storing
    # `agent_input` instead would put a copy of the whole conversation in both.
    # Guarded because a fault here has to degrade to a context-less answer, never to no
    # answer — which is exactly the failure this replaces.
    try:
        agent_input, hist = history.build_agent_input(message, prior_messages)
    except Exception:
        logger.exception("History replay failed — running this turn without context")
        agent_input, hist = message, {}

    note = history.truncation_note(hist)
    if note:
        steps_labels.append(note)
        stream.add_event({"type": "tool_use", "tool_name": "context", "label": note})

    logger.info(
        "[CONTEXT] replay: session=%s turn=%d messages=%s/%s chars=%s truncated=%s",
        session_id,
        turn_idx,
        hist.get("history_messages"),
        hist.get("history_total"),
        hist.get("history_chars"),
        hist.get("history_truncated"),
    )

    turn_t0_ns = 0  # stays 0 if the turn never got a slot; tracing then uses now
    try:
        # Bound concurrent turns: each one spawns a Claude CLI subprocess. We queue
        # rather than reject — the client already has its execution_id and is
        # streaming, so it can be told it is waiting for a slot.
        async with turn_limiter.turn_slot(
            on_queued=lambda ahead: stream.add_event({"type": "queued", "ahead": ahead})
        ) as queued_ms:
            # The user may have pressed Stop while we waited for a slot.
            if stream.is_cancelled:
                raise _TurnCancelled
            turn_t0_ns = time.time_ns()  # after the slot: the queue wait is queued_ms
            async for ev in stream_agent_response(
                message=agent_input,
                system_prompt=system_prompt,
                fmapi_host=fmapi_host,
                fmapi_token=fmapi_token,
                model=GATEWAY,
                # No `session_id=`: a fresh CLI session every turn. Resuming reopened a
                # transcript file on the container disk, which no longer exists after a
                # restart — the context is replayed in `agent_input` instead.
                allowed_tools=allowed_tools,
                mcp_servers=mcp_servers,
                skills_dir=skills_dir,
            ):
                if stream.is_cancelled:
                    break

                ev_type = ev.get("type", "")

                if ev_type == "text_delta":
                    final_text += ev.get("text", "")
                    stream.add_event({"type": "text", "text": final_text})

                elif ev_type == "text" and not final_text:
                    # Guard: text events after text_deltas are redundant SDK summaries.
                    final_text += ev.get("text", "")
                    stream.add_event({"type": "text", "text": final_text})

                elif ev_type == "thinking_delta" or ev_type == "thinking":
                    full_thinking += ev.get("thinking", "")
                    stream.add_event({"type": "thinking", "thinking": full_thinking})

                elif ev_type == "tool_use":
                    tool_name = ev.get("tool_name", "")
                    tool_input = ev.get("tool_input", {})
                    label = _step_label(ev)
                    steps_labels.append(label)
                    stream.add_event({"type": "tool_use", "tool_name": tool_name, "label": label})
                    # Trace tracking
                    step = {
                        "name": tool_name.replace("mcp__databricks__", ""),
                        "input": tool_input,
                        "_t0": time.monotonic(),
                        "_t0_ns": time.time_ns(),  # wall clock: MLflow places spans on it
                        "output": None,
                    }
                    trace_steps.append(step)
                    if ev.get("tool_id"):
                        pending_tools[ev["tool_id"]] = step

                elif ev_type == "tool_result":
                    tid = ev.get("tool_use_id")
                    step = pending_tools.get(tid)
                    if step:
                        step["duration_ms"] = (time.monotonic() - step["_t0"]) * 1000
                        step["_t1_ns"] = time.time_ns()
                        step["output"] = extract_output(step["name"], ev.get("content"), ev.get("is_error"))
                    stream.add_event({"type": "tool_result", "tool_use_id": tid})

                    # Surface Genie Agent Mode reasoning/report in the thoughts area.
                    if step and step["name"].startswith("genie_") and not ev.get("is_error"):
                        full_thinking = (
                            _emit_genie_thinking(stream, ev.get("content"), step["name"], full_thinking)
                            or full_thinking
                        )

                elif ev_type == "result":
                    # Capture the SDK session_id for resume on next turn
                    cli_session_id = ev.get("session_id")
                    cost_meta = {
                        "cost_usd": ev.get("total_cost_usd"),
                        "duration_ms": ev.get("duration_ms"),
                        "duration_api_ms": ev.get("duration_api_ms"),
                        "cli_session_id": cli_session_id,  # = session.id in the CLI telemetry: the join key
                        "num_turns": ev.get("num_turns"),
                    }
                    # Token usage: prefer model_usage (per-model breakdown), fall back to usage
                    model_usage = ev.get("model_usage")
                    if model_usage:
                        cost_meta["input_tokens"] = sum(
                            m.get("inputTokens", 0) for m in model_usage.values()
                        )
                        cost_meta["output_tokens"] = sum(
                            m.get("outputTokens", 0) for m in model_usage.values()
                        )
                        cost_meta["cache_read_tokens"] = sum(
                            m.get("cacheReadInputTokens", 0) for m in model_usage.values()
                        )
                        cost_meta["cache_creation_tokens"] = sum(
                            m.get("cacheCreationInputTokens", 0) for m in model_usage.values()
                        )
                    elif ev.get("usage"):
                        u = ev["usage"]
                        for k in ("input_tokens", "output_tokens"):
                            if u.get(k):
                                cost_meta[k] = u[k]
                                
                    logger.info("[CONTEXT] result: new cli_session_id=%s", cli_session_id)
                    stream.add_event({"type": "result", "meta": cost_meta})

                elif ev_type == "error":
                    error_message = ev.get("error", "Unknown error")
                    stream.add_event({"type": "error", "error": error_message})

                elif ev_type == "keepalive":
                    stream.add_event({"type": "keepalive"})

    except _TurnCancelled:
        logger.info("turn %s cancelled while queued — nothing ran", stream.execution_id)
    except Exception as e:
        logger.exception("Agent background error")
        error_message = str(e)
        stream.add_event({"type": "error", "error": error_message})
    finally:
        # ---- Record cli_session_id (log correlation only, not resume) ----
        # Nothing reads this back to restore context any more — an unread column reads
        # like live resume state to the next person, hence this note. It stays because
        # the CLI prints the same id, making it the join key into `databricks apps logs`.
        if cli_session_id:
            logger.info("[CONTEXT] captured cli_session_id=%s for session=%s", cli_session_id, session_id)

        # Log the MLflow trace BEFORE persisting the message: the message row
        # carries trace_id so that feedback given after a page reload can still be
        # linked to its trace. Writing the message first left trace_id NULL.
        trace_id = None
        try:
            trace_id = await asyncio.to_thread(
                log_turn_trace,
                message,
                final_text,
                trace_steps,
                {
                    "session_id": session_id,
                    "user": user_email,
                    "model": MODEL,
                    # Load context alongside cost/duration, so a slow turn can be read
                    # against how busy the app was when it ran.
                    "turn_started_ns": turn_t0_ns,
                    "turn_ended_ns": time.time_ns(),
                    "concurrent_turns": turn_limiter.in_flight(),
                    "queued_ms": round(queued_ms, 1),
                    # How much conversation this turn was given. Recorded so the token cost
                    # of replay is attributable per turn rather than inferred.
                    **hist,
                    **cost_meta,
                },
            )
        except Exception:
            logger.warning("Trace logging failed", exc_info=True)

        logger.info(
            "[TRACE] session=%s trace_id=%s text_len=%d persisting=%s",
            session_id,
            trace_id,
            len(final_text),
            bool(final_text and db.is_configured()),
        )

        # Persist assistant message to DB
        if final_text and db.is_configured():
            try:
                await asyncio.to_thread(
                    db.add_message,
                    session_id,
                    turn_idx + 1,
                    "assistant",
                    final_text,
                    steps_json=steps_labels,
                    trace_id=trace_id,
                )
            except Exception:
                logger.exception("Failed to persist assistant message")

        # Update CLI session ID in DB, and pin the session's first trace so
        # session-level feedback has something to attach to.
        if db.is_configured():
            updates: dict = {}
            if cli_session_id:
                updates["cli_session_id"] = cli_session_id
            if trace_id and turn_idx == 0:
                updates["first_trace_id"] = trace_id
            if updates:
                try:
                    await asyncio.to_thread(db.update_session, session_id, **updates)
                except Exception:
                    logger.exception("Failed to update session %s", session_id)

        # Upgrade the heuristic title to an LLM one — once per session, and only if the
        # title is still the one we are upgrading. The caller decides whether this turn
        # qualifies (`auto_title_expect is None` means it does not); `turn_idx == 0` is
        # deliberately not the test, because a failed history read fakes it.
        if auto_title_expect is not None and final_text:
            _spawn(
                generate_title_async(
                    session_id=session_id,
                    question=message,
                    answer=final_text,
                    host=fmapi_host,
                    token=fmapi_token,
                    expected_title=auto_title_expect,
                )
            )

        # Compute cost line and tool counts from trace_steps
        genie_count = sum(1 for s in trace_steps if str(s.get("name", "")).startswith("genie_"))
        sql_count = sum(1 for s in trace_steps if str(s.get("name", "")) in ("execute_sql", "get_table_schema"))
        meta_final = {**cost_meta, "genie_count": genie_count, "sql_count": sql_count}

        # Emit complete IMMEDIATELY — don't make the user wait for persistence.
        stream.add_event({"type": "complete", "trace_id": trace_id, "meta": meta_final})
        stream.mark_complete()

        # Terminal status + tail flush. mark_complete() also ends the flush loop,
        # which flushes again; append_execution_events is idempotent.
        if stream.is_cancelled:
            status = "cancelled"
        elif error_message:
            status = "error"
        else:
            status = "complete"
        await execution_store.finish(stream, status, error_message)


def _step_label(ev: dict) -> str:
    """Human-readable one-line label for a tool/skill call."""
    name = ev.get("tool_name", "") or ""
    inp = ev.get("tool_input") or {}
    if name == "Skill":
        sk = inp.get("command") or inp.get("name") or inp.get("skill")
        if not sk and inp:
            sk = str(next(iter(inp.values())))
        return f"\U0001f4da Skill loaded: **{sk or 'skill'}**"
    if name == "mcp__databricks__execute_sql":
        q = " ".join((inp.get("sql_query") or "").split()).replace("`", "\u02cb")
        return f"\U0001f527 SQL: `{q}`"
    if name == "mcp__databricks__get_table_schema":
        return f"\U0001f50d Inspected schema: `{inp.get('catalog')}.{inp.get('schema')}.{inp.get('table')}`"
    if name == "mcp__databricks__generate_report":
        return "\U0001f4c4 Session report generated"
    if name.startswith("mcp__databricks__genie_"):
        space = name.replace("mcp__databricks__genie_", "")
        q = inp.get("question") or ""
        return f"\U0001f9de Genie \u00b7 {space}: {q}"
    return f"\U0001f527 {name.replace('mcp__databricks__', '')}"


def _emit_genie_thinking(stream, content, tool_name: str, existing_thinking: str) -> str | None:
    """Surface Genie Agent Mode reasoning/report in the thoughts panel.

    Parses the tool result JSON and emits a `thinking` event with
    the Genie research plan (reasoning) and/or final report.
    Returns the updated thinking string (for accumulation across calls).

    `content` can be:
      - a string (pre-processed by _process_tool_result in agent_loop.py)
      - a list of MCP content blocks [{"type": "text", "text": "..."}]
    """
    if not content:
        return None

    # Resolve text_payload from either format.
    text_payload = ""
    if isinstance(content, str):
        # Already a string (the common path via _process_tool_result).
        text_payload = content
    elif isinstance(content, list):
        # Raw MCP content blocks (fallback).
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_payload = block.get("text", "")
                break

    if not text_payload:
        return None
    try:
        data = json.loads(text_payload)
    except (json.JSONDecodeError, TypeError):
        return None

    space = tool_name.replace("genie_", "")
    parts: list[str] = []

    reasoning = data.get("reasoning")
    if reasoning:
        parts.append(f"**\U0001f9de Genie ({space}) — Research Plan:**\n{reasoning}")

    queries = data.get("queries") or []
    if queries:
        sql_lines = []
        for q in queries:
            title = q.get("title") or "Query"
            sql = q.get("sql") or "(no SQL)"
            row_count = len(q.get("rows") or [])
            sql_lines.append(f"- {title}: `{sql[:120]}` → {row_count} rows")
        parts.append("**Queries executed:**\n" + "\n".join(sql_lines))

    report = data.get("report")
    if report:
        parts.append(f"**Genie Report:**\n{report}")

    if parts:
        genie_thought = "\n\n".join(parts)
        # Append to existing thinking so it accumulates.
        combined = (existing_thinking + "\n\n" + genie_thought).strip()
        stream.add_event({"type": "thinking", "thinking": combined})
        return combined
    return None
