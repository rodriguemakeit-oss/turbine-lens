"""Turbine Lens database engine + token management (Lakebase/Postgres).

Sync SQLAlchemy + psycopg3. Callers wrap DAO calls in asyncio.to_thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import URL, create_engine, event, func, inspect, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    SCHEMA,
    Base,
    ChartSpecRow,
    ExecutionEventRow,
    ExecutionRow,
    FeedbackRow,
    MessageRow,
    SessionSettingRow,
    ReportRow,
    SessionRow,
)

logger = logging.getLogger(__name__)

_REFRESH_SECONDS = 50 * 60


# --- Connection params -----------------------------------------------------
def _pg_host() -> str | None:
    return os.environ.get("PGHOST")


def _pg_user() -> str | None:
    return os.environ.get("PGUSER")


def _pg_database() -> str:
    return os.environ.get("PGDATABASE") or os.environ.get("LAKEBASE_DATABASE_NAME", "databricks_postgres")


def _pg_port() -> int:
    return int(os.environ.get("PGPORT", "5432"))


def _instance_name() -> str | None:
    return os.environ.get("LAKEBASE_INSTANCE_NAME")


def is_configured() -> bool:
    """True when Lakebase binding is present."""
    return bool(_pg_host() and _pg_user() and _instance_name())


# --- Engine + token refresh ------------------------------------------------
_engine = None
_session_maker: sessionmaker | None = None
_lock = threading.Lock()
_token: str | None = None
_token_ts: float = 0.0


def _mint_token() -> str:
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    cred = client.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[_instance_name()],
    )
    return cred.token


def _current_token() -> str:
    global _token, _token_ts
    now = time.time()
    if _token is None or (now - _token_ts) > _REFRESH_SECONDS:
        _token = _mint_token()
        _token_ts = now
        logger.info("Minted fresh Lakebase token")
    return _token


def get_engine():
    global _engine, _session_maker
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        if not is_configured():
            raise RuntimeError("Lakebase not configured.")
        url = URL.create(
            "postgresql+psycopg",
            username=_pg_user(),
            password="",
            host=_pg_host(),
            port=_pg_port(),
            database=_pg_database(),
        )
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=_REFRESH_SECONDS - 600,
            pool_size=5,
            max_overflow=10,
            connect_args={
                "sslmode": "require",
                "options": f"-c search_path={SCHEMA},public",
            },
        )

        @event.listens_for(engine, "do_connect")
        def _provide_token(dialect, conn_rec, cargs, cparams):
            cparams["password"] = _current_token()

        _engine = engine
        _session_maker = sessionmaker(bind=engine, expire_on_commit=False)
        return _engine


@contextmanager
def get_session() -> Iterator[Session]:
    get_engine()
    s = _session_maker()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


#: Conversation storage. Without these the app has no memory worth the name.
_CORE_TABLES = (SessionRow, MessageRow, FeedbackRow)
#: Session-level settings (e.g. genie mode). Nice-to-have: absence falls back to
#: global defaults rather than taking conversation storage down.
_SETTINGS_TABLES = (SessionSettingRow,)
#: Turn durability (reattach after reload, partial output after a restart). A
#: nice-to-have: the app is fully usable without it.
_EXECUTION_TABLES = (ExecutionRow, ExecutionEventRow)
#: Chart durability (charts in older answers survive a restart). Also a nice-to-have:
#: without it charts still render for the life of the container that drew them.
_CHART_TABLES = (ChartSpecRow,)
#: Session reports. Independent again: without the table the app is fully usable, it
#: simply cannot produce a report.
_REPORT_TABLES = (ReportRow,)

_healthy = False
_settings_available = False
_executions_available = False
_charts_available = False
_reports_available = False


def is_healthy() -> bool:
    """True once bootstrap() has confirmed conversation storage is usable.

    `is_configured()` only reports that the connection env vars are present.
    Anything that needs to know whether persistence actually *works* — the UI
    especially — must ask this, otherwise a degraded fallback to the volatile
    in-memory store reports itself as healthy and sessions vanish silently.
    """
    return _healthy


def session_settings_available() -> bool:
    """True when session settings table exists and is writable."""
    return _healthy and _settings_available


def executions_available() -> bool:
    """True when the execution tables exist and are writable."""
    return _healthy and _executions_available


def charts_available() -> bool:
    """True when the chart-spec table exists and is writable."""
    return _healthy and _charts_available


def reports_available() -> bool:
    """True when the reports table exists and is writable."""
    return _healthy and _reports_available


def _create_missing(eng, tables, have: set[str]) -> set[str]:
    missing = {t.__tablename__ for t in tables} - have
    if missing:
        Base.metadata.create_all(eng, tables=[t for t in Base.metadata.sorted_tables if t.name in missing])
        logger.info("Created tables: %s", missing)
    return missing


def bootstrap() -> None:
    """Ensure schema/tables exist. Idempotent.

    Check-then-act on purpose: the app service principal holds DML grants but does
    not necessarily *own* these tables, and CREATE/ALTER need ownership that a
    GRANT does not confer (a recreated app gets a brand-new SP that owns nothing).
    So we only ever create what is genuinely absent.

    The settings, execution and chart tables are bootstrapped separately and their
    failure is contained: they are worth having, but not at the price of taking
    conversation storage down with them if the SP cannot create them.
    """
    global _healthy, _settings_available, _executions_available, _charts_available, _reports_available
    eng = get_engine()
    insp = inspect(eng)
    if SCHEMA not in insp.get_schema_names():
        with eng.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        insp = inspect(eng)
    have = set(insp.get_table_names(schema=SCHEMA))

    if not _create_missing(eng, _CORE_TABLES, have):
        logger.info("All core tables present in schema %s", SCHEMA)
    _healthy = True

    try:
        _create_missing(eng, _SETTINGS_TABLES, have)
        _settings_available = True
    except Exception:
        logger.exception(
            "Session settings table unavailable — per-session overrides disabled "
            "(falling back to global defaults). Conversation storage is unaffected."
        )

    try:
        _create_missing(eng, _EXECUTION_TABLES, have)
        _executions_available = True
    except Exception:
        logger.exception(
            "Execution tables unavailable — turn durability disabled (reload will "
            "not reattach to a running turn). Conversation storage is unaffected."
        )

    try:
        _create_missing(eng, _CHART_TABLES, have)
        _charts_available = True
    except Exception:
        logger.exception(
            "Chart table unavailable — chart durability disabled (charts render for "
            "the life of this container only). Conversation storage is unaffected."
        )

    try:
        _create_missing(eng, _REPORT_TABLES, have)
        _reports_available = True
    except Exception:
        logger.exception("Reports table unavailable — session reports disabled. Everything else is unaffected.")


# --- DAO functions ---------------------------------------------------------
def create_session(session_id: str, owner_email: str, owner_name: str | None = None, title: str | None = None) -> dict:
    with get_session() as s:
        row = SessionRow(
            id=session_id,
            owner_email=owner_email,
            owner_name=owner_name,
            title=title,
        )
        s.add(row)
    return {"id": session_id, "title": title}


def update_session(session_id: str, touch: bool = True, **kwargs) -> bool:
    """Patch session fields.

    `touch=False` leaves `updated_at` alone. Sessions are listed and dated by that
    column, so a rename would otherwise jump a months-old conversation to the top of
    the sidebar claiming it was active a minute ago — the conversation did not change,
    only its name did.
    """
    with get_session() as s:
        row = s.get(SessionRow, session_id)
        if not row:
            return False
        for k, v in kwargs.items():
            if hasattr(row, k):
                setattr(row, k, v)
        if touch:
            row.updated_at = datetime.now(timezone.utc)
    return True


def set_title_if_unchanged(session_id: str, expected: str | None, new_title: str) -> bool:
    """Set the title only if it is still `expected`. False means someone got there first.

    This is the guard on the background auto-titler: a user who dislikes the heuristic
    title renames the session while the first answer is still streaming, and the titler
    would land afterwards and overwrite that rename with no error anywhere. It is one
    atomic `UPDATE ... WHERE title = expected` on purpose — a read-then-write would lose
    a rename committed between the two statements.
    """
    with get_session() as s:
        res = s.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.title == expected)
            .values(title=new_title, updated_at=datetime.now(timezone.utc))
        )
        return bool(res.rowcount)


def list_sessions(limit: int = 100) -> list[dict]:
    with get_session() as s:
        rows = s.execute(select(SessionRow).order_by(SessionRow.updated_at.desc()).limit(limit)).scalars().all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "owner_email": r.owner_email,
                "message_count": r.message_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


def get_session_meta(session_id: str) -> dict | None:
    """The session row without its messages — ownership checks and the UI banner."""
    with get_session() as s:
        row = s.get(SessionRow, session_id)
        if not row:
            return None
        return {
            "id": row.id,
            "title": row.title,
            "owner_email": row.owner_email,
            "owner_name": row.owner_name,
            "message_count": row.message_count,
            # Session-level feedback attaches to the session's FIRST trace, not its
            # latest answer — see the feedback router.
            "first_trace_id": row.first_trace_id,
        }


def get_session_messages(session_id: str) -> list[dict]:
    with get_session() as s:
        rows = (
            s.execute(select(MessageRow).where(MessageRow.session_id == session_id).order_by(MessageRow.turn_index))
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "role": r.role,
                "content": r.content,
                "steps_json": r.steps_json,
                "trace_id": r.trace_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def get_cli_session_id(session_id: str) -> str | None:
    """Return the stored CLI session ID for a given session (for resume)."""
    with get_session() as s:
        row = s.get(SessionRow, session_id)
        return row.cli_session_id if row else None


def add_message(
    session_id: str, turn_index: int, role: str, content: str, steps_json=None, trace_id: str | None = None
) -> int:
    with get_session() as s:
        row = MessageRow(
            session_id=session_id,
            turn_index=turn_index,
            role=role,
            content=content,
            steps_json=steps_json,
            trace_id=trace_id,
        )
        s.add(row)
        # Update session
        sess = s.get(SessionRow, session_id)
        if sess:
            sess.message_count = (sess.message_count or 0) + 1
            sess.updated_at = datetime.now(timezone.utc)
        s.flush()
        return row.id


def delete_session(session_id: str) -> bool:
    with get_session() as s:
        s.execute(MessageRow.__table__.delete().where(MessageRow.session_id == session_id))
        s.execute(SessionSettingRow.__table__.delete().where(SessionSettingRow.session_id == session_id))
        # reports.session_id IS a foreign key, so these must go before the session row or
        # the delete is refused outright.
        s.execute(
            ReportRow.__table__.delete().where(ReportRow.session_id == session_id)
        )  # executions.session_id is not a FK (see models.py), so cascade by hand
        # or the rows outlive the session they belong to.
        exec_ids = list(s.execute(select(ExecutionRow.id).where(ExecutionRow.session_id == session_id)).scalars().all())
        if exec_ids:
            s.execute(ExecutionEventRow.__table__.delete().where(ExecutionEventRow.execution_id.in_(exec_ids)))
            s.execute(ExecutionRow.__table__.delete().where(ExecutionRow.session_id == session_id))
        row = s.get(SessionRow, session_id)
        if row:
            s.delete(row)
            return True
    return False


def get_session_genie_mode(session_id: str) -> str | None:
    if not session_settings_available():
        return None
    with get_session() as s:
        row = s.get(SessionSettingRow, session_id)
        return row.genie_mode if row and row.genie_mode else None


def set_session_genie_mode(session_id: str, mode: str) -> None:
    if not session_settings_available():
        return
    with get_session() as s:
        stmt = (
            pg_insert(SessionSettingRow.__table__)
            .values(
                session_id=session_id,
                genie_mode=mode,
                updated_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    "genie_mode": mode,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        s.execute(stmt)


# --- Executions (turn durability) -----------------------------------------
def create_execution(execution_id: str, session_id: str, owner_email: str | None) -> None:
    with get_session() as s:
        s.add(
            ExecutionRow(
                id=execution_id,
                session_id=session_id,
                owner_email=owner_email,
                status="running",
            )
        )


def append_execution_events(execution_id: str, start_seq: int, payloads: list[dict]) -> int:
    """Insert events at seq [start_seq, start_seq+len). Returns the new high-water mark.

    ON CONFLICT DO NOTHING makes a retried flush idempotent rather than fatal:
    the (execution_id, seq) primary key already identifies each event.
    """
    if not payloads:
        return start_seq
    with get_session() as s:
        s.execute(
            pg_insert(ExecutionEventRow.__table__)
            .values(
                [{"execution_id": execution_id, "seq": start_seq + i, "payload": p} for i, p in enumerate(payloads)]
            )
            .on_conflict_do_nothing(index_elements=["execution_id", "seq"])
        )
        row = s.get(ExecutionRow, execution_id)
        if row:
            row.event_count = start_seq + len(payloads)
            row.updated_at = datetime.now(timezone.utc)
    return start_seq + len(payloads)


def finish_execution(execution_id: str, status: str, error: str | None = None) -> None:
    with get_session() as s:
        row = s.get(ExecutionRow, execution_id)
        if row:
            row.status = status
            row.error = error
            row.updated_at = datetime.now(timezone.utc)


def get_latest_execution(session_id: str) -> dict | None:
    """The most recent execution for a session, whatever its status.

    Deliberately not filtered to 'running': the startup sweep relabels a turn
    killed by a restart as 'interrupted', and filtering on 'running' would hide
    exactly the partial output the mirror exists to preserve.
    """
    with get_session() as s:
        row = (
            s.execute(
                select(ExecutionRow)
                .where(ExecutionRow.session_id == session_id)
                .order_by(ExecutionRow.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if not row:
            return None
        return {
            "execution_id": row.id,
            "status": row.status,
            "event_count": row.event_count,
            "owner_email": row.owner_email,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


def get_execution_events(execution_id: str, after_seq: int = 0) -> list[dict]:
    with get_session() as s:
        return list(
            s.execute(
                select(ExecutionEventRow.payload)
                .where(
                    ExecutionEventRow.execution_id == execution_id,
                    ExecutionEventRow.seq >= after_seq,
                )
                .order_by(ExecutionEventRow.seq)
            )
            .scalars()
            .all()
        )


def execution_load_stats(days: int = 7) -> dict:
    """Turn counts and peak *in-flight* turns over a window.

    Derived from start/end times already recorded per execution — no extra
    instrumentation needed. Implemented as a sweep over start(+1)/end(-1) events with a
    running sum, rather than a correlated "how many overlap me" subquery, so it stays
    O(n log n) as the table grows.

    Counts turns **in flight**, which includes those waiting for a concurrency slot:
    the row is written when the turn is accepted, before the slot is acquired. So this
    can legitimately exceed the cap, and is a different measure from the live
    `peak_since_restart`, which counts turns actually executing. Both are useful —
    in-flight is what a user experiences, executing is what loads the container.
    """
    peak_sql = text(f"""
        WITH ev AS (
            SELECT created_at AS t, 1 AS d FROM {SCHEMA}.executions
             WHERE created_at > now() - make_interval(days => :days)
            UNION ALL
            SELECT COALESCE(updated_at, created_at) AS t, -1 AS d FROM {SCHEMA}.executions
             WHERE created_at > now() - make_interval(days => :days)
        ), run AS (
            SELECT SUM(d) OVER (ORDER BY t, d DESC) AS running FROM ev
        )
        SELECT COALESCE(MAX(running), 0) FROM run
    """)
    counts_sql = text(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status = 'running')     AS running,
               count(*) FILTER (WHERE status = 'interrupted') AS interrupted,
               count(*) FILTER (WHERE status = 'error')       AS errored,
               count(*) FILTER (WHERE status = 'cancelled')   AS cancelled,
               COALESCE(AVG(EXTRACT(EPOCH FROM (updated_at - created_at))), 0) AS avg_seconds
          FROM {SCHEMA}.executions
         WHERE created_at > now() - make_interval(days => :days)
    """)
    with get_session() as s:
        peak = s.execute(peak_sql, {"days": days}).scalar() or 0
        row = s.execute(counts_sql, {"days": days}).mappings().first() or {}
    return {
        "window_days": days,
        # Named for what it measures: accepted-and-not-yet-finished, queued included.
        # May exceed the cap; compare with live.peak_since_restart for executing turns.
        "peak_turns_in_flight": int(peak),
        "turns": int(row.get("total") or 0),
        "running": int(row.get("running") or 0),
        "interrupted": int(row.get("interrupted") or 0),
        "errored": int(row.get("errored") or 0),
        "cancelled": int(row.get("cancelled") or 0),
        "avg_turn_seconds": round(float(row.get("avg_seconds") or 0), 1),
    }


def sweep_orphaned_executions() -> int:
    """Mark every still-'running' execution as interrupted. Returns the count.

    Called once at startup. Nothing can legitimately be running before the
    process that would run it exists, so any such row was orphaned by a restart
    or a crash — and left alone it makes a reloaded page wait forever on a turn
    that will never emit another event.

    This relies on the single-instance invariant (CLAUDE.md #7): with more than
    one replica, this would wrongly sweep another live replica's turns.
    """
    with get_session() as s:
        res = s.execute(
            ExecutionRow.__table__.update()
            .where(ExecutionRow.status == "running")
            .values(status="interrupted", updated_at=datetime.now(timezone.utc))
        )
        return res.rowcount or 0


# --- Chart specs (chart durability) ---------------------------------------
def upsert_chart_spec(name: str, spec: dict) -> None:
    """Mirror one chart spec. Idempotent by name, so a retried write is not fatal."""
    with get_session() as s:
        s.execute(
            pg_insert(ChartSpecRow.__table__)
            .values(name=name, spec=spec)
            .on_conflict_do_nothing(index_elements=["name"])
        )


def get_chart_spec(name: str) -> dict | None:
    with get_session() as s:
        row = s.get(ChartSpecRow, name)
        return row.spec if row else None


def chart_messages() -> list[dict]:
    """Traced assistant answers that embed a chart fence — input to the backfill.

    Filtered in SQL rather than by scanning every message in Python: the fence text is
    the only marker, and a trace id is what makes a row recoverable at all.
    """
    with get_session() as s:
        rows = s.execute(
            select(MessageRow.trace_id, MessageRow.content)
            .where(
                MessageRow.role == "assistant",
                MessageRow.trace_id.isnot(None),
                or_(
                    MessageRow.content.like("%```echarts%"),
                    MessageRow.content.like("%```plotly%"),
                ),
            )
            .order_by(MessageRow.id)
        ).all()
        return [{"trace_id": r[0], "content": r[1]} for r in rows]


def existing_chart_names(names: list[str]) -> set[str]:
    """Which of these chart specs are already stored (so the backfill can skip them)."""
    if not names:
        return set()
    with get_session() as s:
        return set(s.execute(select(ChartSpecRow.name).where(ChartSpecRow.name.in_(names))).scalars().all())


def delete_chart_specs(names: list[str]) -> int:
    if not names:
        return 0
    with get_session() as s:
        res = s.execute(ChartSpecRow.__table__.delete().where(ChartSpecRow.name.in_(names)))
        return res.rowcount or 0


# --- Session reports ------------------------------------------------------
def create_report(session_id: str, payload: dict, message_count: int, created_by: str | None, render_html) -> dict:
    """Store a new report version. Returns {id, version, created_at}.

    `render_html(version) -> str` is called **inside** the transaction, because the version
    number is printed in the document: rendering before the number is known would either
    guess it or need a second write, and a reader must never see a version that misstates
    itself. The unique constraint is the real guard — two people generating at once would
    otherwise read the same max, and the loser re-renders rather than overwrites, since
    versions are immutable.
    """
    import uuid as _uuid

    for attempt in (1, 2, 3):
        try:
            with get_session() as s:
                current = s.execute(
                    select(func.max(ReportRow.version)).where(ReportRow.session_id == session_id)
                ).scalar()
                version = (current or 0) + 1
                row = ReportRow(
                    id=_uuid.uuid4().hex,
                    session_id=session_id,
                    version=version,
                    payload=payload,
                    html=render_html(version),
                    message_count=message_count,
                    created_by=created_by,
                )
                s.add(row)
                s.flush()
                return {
                    "id": row.id,
                    "version": row.version,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
        except Exception:
            if attempt == 3:
                raise
            logger.warning("report version collision for %s — re-rendering", session_id)
    raise RuntimeError("unreachable")


def get_report(report_id: str) -> dict | None:
    with get_session() as s:
        row = s.get(ReportRow, report_id)
        if not row:
            return None
        return {
            "id": row.id,
            "session_id": row.session_id,
            "version": row.version,
            "payload": row.payload,
            "html": row.html,
            "message_count": row.message_count,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


def list_reports(session_id: str) -> list[dict]:
    """Versions for a session, newest first. Without the HTML — this feeds a picker."""
    with get_session() as s:
        rows = s.execute(
            select(ReportRow.id, ReportRow.version, ReportRow.created_at, ReportRow.created_by, ReportRow.message_count)
            .where(ReportRow.session_id == session_id)
            .order_by(ReportRow.version.desc())
        ).all()
        return [
            {
                "id": r[0],
                "version": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
                "created_by": r[3],
                "message_count": r[4],
            }
            for r in rows
        ]


def add_feedback(
    session_id: str | None,
    trace_id: str | None,
    level: str,
    rater_email: str,
    rating: str,
    reason: str | None = None,
    comment: str | None = None,
) -> int:
    with get_session() as s:
        row = FeedbackRow(
            session_id=session_id,
            trace_id=trace_id,
            level=level,
            rater_email=rater_email,
            rating=rating,
            reason=reason,
            comment=comment,
        )
        s.add(row)
        s.flush()
        return row.id
