"""MLflow GenAI tracing for Turbine Lens turns (R5).

One trace per turn: a root AGENT span (question/answer + session/user/model/cost
metadata) and a child TOOL span per tool call. Tool input is kept in full (incl.
the SQL query / Genie question); tool OUTPUT is limited to genie_sql + row_count +
status + a trimmed summary — NEVER the result rows (per the agreed trace policy).
Logged from the app service principal to the bound MLflow experiment.

Note: traces export asynchronously, so we `flush_trace_async_logging()` before
returning the trace_id (feedback attaches by trace_id later — otherwise the
backend returns NOT_FOUND). Every function is best-effort and guarded — tracing
must NEVER break a turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Guards MLflow's fluent span API, which is not safe to drive from several turns at
#: once — see the comment in log_turn_trace().
_trace_lock = threading.Lock()

_CHECKED = False
_OK = False

def bootstrap() -> None:
    """Set the MLflow experiment for tracing, if configured. Called at app startup."""
    if not is_enabled():
        return
    import mlflow

    exp_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
    exp_name = os.environ.get("MLFLOW_EXPERIMENT_NAME")
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "databricks"))

    if exp_id:
        mlflow.set_experiment(experiment_id=exp_id)
    else:
        mlflow.set_experiment(experiment_name=exp_name)

        # Log the trace_location for diagnostics (UC or None)
    try:
        exp = mlflow.get_experiment(exp_id) if exp_id else mlflow.get_experiment_by_name(exp_name)
        if exp and exp.trace_location:
            logger.info(
                "MLflow experiment %s -> trace_location: %s",
                exp.experiment_id, exp.trace_location,
            )
    except Exception:  # noqa: BLE001
        logger.debug("Could not read trace_location from experiment", exc_info=True)


def is_enabled() -> bool:
    """True when an experiment is configured and mlflow imports. Cached."""
    global _CHECKED, _OK
    if not _CHECKED:
        _CHECKED = True
        if not (os.environ.get("MLFLOW_EXPERIMENT_ID") or os.environ.get("MLFLOW_EXPERIMENT_NAME")):
            _OK = False
        else:
            try:
                import mlflow  # noqa: F401

                _OK = True
            except Exception:  # noqa: BLE001
                logger.warning("mlflow not importable; tracing disabled", exc_info=True)
                _OK = False
    return _OK


def extract_output(tool_name: str, content: Optional[str], is_error: bool) -> dict:
    """Distill a tool result into the keep-fields — no result rows, no DDL."""
    name = tool_name or ""
    out: dict[str, Any] = {"status": "error" if is_error else "ok"}
    if is_error:
        out["error"] = (content or "")[:500]
        return out
    if "genie_" in name:
        try:
            d = json.loads(content) if content else {}
            for k in ("space", "genie_sql", "conversation_id"):
                if d.get(k):
                    out[k] = d[k]
            out["row_count"] = len(d.get("rows") or [])
            ts = d.get("text_summary")
            if ts:
                out["text_summary"] = str(ts)[:500]
        except Exception:  # noqa: BLE001
            out["note"] = "unparsed genie output"
    elif name.endswith("resolve_site"):
        # Keep the outcome, not the candidate list: whether the human check fired is
        # the thing worth being able to count over a week of traces.
        try:
            d = json.loads(content) if content else {}
            out["resolve_status"] = d.get("status")
            out["needs_user_choice"] = bool(d.get("needs_user_choice"))
            for k in ("farm", "farm_code", "matched_on", "match_count"):
                if d.get(k) is not None:
                    out[k] = d[k]
        except Exception:  # noqa: BLE001
            out["note"] = "unparsed resolve_site output"
    elif name.endswith("execute_sql"):
        m = re.search(r"\((\d+)\s+rows?\)", content or "")
        out["row_count"] = int(m.group(1)) if m else None
    elif name.endswith("get_table_schema"):
        out["note"] = "schema returned (DDL omitted)"
    return out


def log_turn_trace(question: str, answer: str, steps: list[dict], meta: dict, flush: bool = True) -> Optional[str]:
    """Build + log the turn's trace; return its trace_id (or None on any failure).

    Set flush=False to skip the (slow, network) flush and return trace_id immediately
    — the caller can defer flush() to a background task off the turn's critical path."""
    if not is_enabled():
        return None
    try:
        import mlflow
        from mlflow.entities import SpanType
        mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "databricks"))

        exp = os.environ.get("MLFLOW_EXPERIMENT_ID")
        if exp:
            mlflow.set_experiment(experiment_id=exp)

        # Serialised deliberately. mlflow.start_span() drives global fluent state, and
        # concurrent turns racing it make MLflow hand back a NoOpSpan — a dummy that
        # raises nothing and has no trace_id. Measured: 7 simultaneous turns produced
        # 3 traces and 4 silent blanks, which then left those answers unrateable
        # because feedback was keyed on trace_id. Callers are already on worker
        # threads (asyncio.to_thread), so a plain Lock is the right primitive; the cost
        # is that a few turns finishing at once log their traces one after another.
        with _trace_lock:
            _in = (meta or {}).get("input_tokens")
            _out = (meta or {}).get("output_tokens")
            md = {}
            try:  # RESERVED metadata keys → groups traces in the MLflow "Sessions" view
                md = {}
                if (meta or {}).get("session_id"):
                    md["mlflow.trace.session"] = str(meta["session_id"])
                if (meta or {}).get("user"):
                    md["mlflow.trace.user"] = str(meta["user"])
                if (meta or {}).get("cli_session_id"):
                    md["turbine_lens.cli_session_id"] = str(meta["cli_session_id"])

                # Trace-level token usage → fills the Tokens column in the trace list.
                if _in or _out:
                    trace_usage_entry = {
                        "model": (meta or {}).get("model", "unknown"),
                        "input_tokens": _in or 0,
                        "output_tokens": _out or 0,
                        "total_tokens": (_in or 0) + (_out or 0),
                    }
                    md["mlflow.trace.tokenUsage"] = json.dumps([trace_usage_entry])
                # Cost → separate reserved key (mlflow.trace.cost), NOT inside tokenUsage.
                # Gateway-reported total_cost_usd is ground truth (model absent from LiteLLM).
                if (meta or {}).get("cost_usd"):
                    md["mlflow.trace.cost"] = json.dumps({
                        "total_cost": meta["cost_usd"],
                    })
            except Exception:  # noqa: BLE001
                logger.debug("trace metadata failed", exc_info=True)
            # Explicit timestamps: the trace is written after the turn, so without them
            # every span carried the instant of the write (~5 ms for a turn of minutes).
            # Metadata is passed here because update_current_trace() needs a current span.
            root = mlflow.start_span_no_context(
                name="turbine_lens_turn", span_type=SpanType.AGENT, metadata=md or None,
                start_time_ns=(meta or {}).get("turn_started_ns") or None,
            )
            root.set_inputs({"question": question})
            root.set_attributes({k: v for k, v in (meta or {}).items() if v is not None})

            # Set mlflow.llm.model with an "endpoints:/" prefix so the model
            # name appears in the MLflow UI column, while _SKIP_COST_PREFIXES
            # (tracing/utils/__init__.py:348-350) causes calculate_span_cost to
            # bail out before any LiteLLM pricing lookup. Our gateway-reported
            # cost_usd stays as ground truth — no overwrite.
            if (meta or {}).get("model"):
                root.set_attribute("mlflow.llm.model", f"endpoints:/{meta['model']}")
                root.set_attribute("mlflow.llm.provider", "databricks")
            # Populate the reserved token-usage key so the MLflow UI columns fill.
            # input_tokens/output_tokens arrive from model_usage (gateway-reported).
            if _in or _out:
                token_usage = {
                    "input_tokens": _in or 0,
                    "output_tokens": _out or 0,
                    "total_tokens": (_in or 0) + (_out or 0),
                }
                # Include cache breakdown when present (informational)
                if (meta or {}).get("cache_read_tokens"):
                    token_usage["cache_read_input_tokens"] = meta["cache_read_tokens"]
                if (meta or {}).get("cache_creation_tokens"):
                    token_usage["cache_creation_input_tokens"] = meta["cache_creation_tokens"]
                try:
                    root.set_attribute("mlflow.chat.tokenUsage", token_usage)
                except Exception:  # noqa: BLE001
                    logger.debug("set mlflow.chat.tokenUsage failed", exc_info=True)
            # Cost: mlflow.llm.cost is the reserved span attribute for the UI Cost column.
            # Gateway-reported total_cost_usd is the ground truth (model not in LiteLLM table).
            if (meta or {}).get("cost_usd"):
                try:
                    root.set_attribute("mlflow.llm.cost", {
                        "total_cost": meta["cost_usd"],
                    })
                except Exception:  # noqa: BLE001
                    logger.debug("set mlflow.llm.cost failed", exc_info=True)

            try:
                for s in steps or []:
                    sp = mlflow.start_span_no_context(
                        name=s.get("name") or "tool", span_type=SpanType.TOOL, parent_span=root,
                        inputs=s.get("input"), start_time_ns=s.get("_t0_ns") or None,
                        attributes={"duration_ms": round(s["duration_ms"])} if s.get("duration_ms") is not None else None,
                    )
                    sp.end(outputs=s.get("output"), end_time_ns=s.get("_t1_ns") or None)
            finally:
                root.end(outputs={"answer": answer}, end_time_ns=(meta or {}).get("turn_ended_ns") or None)
            trace_id = root.trace_id
            if not trace_id:
                # NoOpSpan: MLflow declined to start a trace and said nothing about it.
                # Log it, because the previous silence meant answers quietly became
                # unrateable with no clue as to why.
                logger.warning(
                    "MLflow returned no trace_id (NoOp span) — this turn will not be "
                    "linkable to a trace; feedback still records against the session"
                )

        if flush:
            try:
                mlflow.flush_trace_async_logging()  # else feedback by trace_id → NOT_FOUND
            except Exception:  # noqa: BLE001
                logger.debug("flush_trace_async_logging unavailable", exc_info=True)
        return trace_id
    except Exception:  # noqa: BLE001
        logger.exception("log_turn_trace failed")
        return None


def flush() -> None:
    """Flush buffered traces to the backend so feedback-by-trace_id resolves. Called
    from a background task (off the turn's critical path) when log_turn_trace(flush=False)."""
    if not is_enabled():
        return
    try:
        import mlflow

        mlflow.flush_trace_async_logging()
    except Exception:  # noqa: BLE001
        logger.debug("flush unavailable", exc_info=True)


def log_feedback(
    trace_id: Optional[str],
    name: str,
    value: Any,
    rationale: Optional[str] = None,
    source_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Attach a human assessment to a trace (best-effort). The trace must already
    be logged + flushed (it is — we flush at end of turn). `metadata` carries the
    reserved `mlflow.trace.session` key for session-level (vs answer-level) ratings.
    Returns 'ok' | 'ok_no_metadata' | 'disabled' | an error string (for diag)."""
    if not (trace_id and is_enabled()):
        return "disabled"
    try:
        import mlflow
        from mlflow.entities import AssessmentSource

        src = AssessmentSource(source_type="HUMAN", source_id=source_id or "unknown")
        kw = dict(trace_id=trace_id, name=name, value=value, rationale=rationale or None, source=src)
        if metadata:
            try:
                mlflow.log_feedback(metadata=metadata, **kw)
                return "ok"
            except TypeError:  # this mlflow build's log_feedback lacks metadata=
                logger.debug("log_feedback has no metadata kwarg; logging without it")
                mlflow.log_feedback(**kw)
                return "ok_no_metadata"
        mlflow.log_feedback(**kw)
        return "ok"
    except Exception as e:  # noqa: BLE001
        logger.exception("log_feedback failed")
        return f"{type(e).__name__}: {e}"
