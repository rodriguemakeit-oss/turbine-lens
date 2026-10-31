"""SQLAlchemy ORM models for Turbine Lens persistence (Lakebase/Postgres)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

SCHEMA = os.environ.get("LAKEBASE_SCHEMA_NAME", "turbine_lens")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": SCHEMA}

    id = mapped_column(Text, primary_key=True)
    owner_email = mapped_column(Text, nullable=False)
    owner_name = mapped_column(Text)
    title = mapped_column(Text)
    cli_session_id = mapped_column(Text)
    first_trace_id = mapped_column(Text)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    message_count = mapped_column(Integer, nullable=False, default=0)


class SessionSettingRow(Base):
    __tablename__ = "session_settings"
    __table_args__ = {"schema": SCHEMA}

    session_id = mapped_column(
        Text,
        ForeignKey(f"{SCHEMA}.sessions.id"),
        primary_key=True,
    )
    genie_mode = mapped_column(String(16), nullable=True)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = {"schema": SCHEMA}

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id = mapped_column(Text, ForeignKey(f"{SCHEMA}.sessions.id"), nullable=False, index=True)
    turn_index = mapped_column(Integer, nullable=False)
    role = mapped_column(String(16), nullable=False)
    content = mapped_column(Text, nullable=False)
    steps_json = mapped_column(JSONB)
    trace_id = mapped_column(Text)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ExecutionRow(Base):
    """One agent turn's execution state, so a turn survives losing its viewer.

    The turn runs as a background task and streams into an in-process buffer;
    this row (plus ExecutionEventRow) is the durable record, so a reloaded page
    can discover a turn already in flight, and a turn killed by a restart leaves
    its partial output behind instead of vanishing.
    """

    __tablename__ = "executions"
    __table_args__ = {"schema": SCHEMA}

    id = mapped_column(Text, primary_key=True)  # execution_id (uuid)
    # Intentionally NOT a foreign key: a degraded-persistence turn may have no
    # session row, and we still want its events recorded rather than rejected.
    session_id = mapped_column(Text, nullable=False, index=True)
    owner_email = mapped_column(Text)
    # running | complete | cancelled | error | interrupted
    status = mapped_column(String(16), nullable=False, default="running")
    error = mapped_column(Text)
    event_count = mapped_column(Integer, nullable=False, default=0)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ExecutionEventRow(Base):
    """Append-only stream events.

    `seq` is the same zero-based index the client already sends as
    `last_event_index`, so reattaching is a cursor read rather than a diff. The
    composite primary key also makes a re-flushed batch idempotent.
    """

    __tablename__ = "execution_events"
    __table_args__ = {"schema": SCHEMA}

    execution_id = mapped_column(Text, primary_key=True)
    seq = mapped_column(Integer, primary_key=True)
    payload = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ChartSpecRow(Base):
    """A rendered chart's spec, so a chart outlives the container that drew it.

    The chart tool writes the spec to TURBINE_LENS_CHART_DIR and puts that *path* in the
    answer text. The answer is durable and the file is not, so before this table every
    chart in every older conversation turned into a 404 iframe at the next restart.
    Disk stays the cache; this is the source of truth.

    No `kind` column on purpose: routers/charts.py already infers echarts-vs-plotly from
    the name prefix and the spec's shape, and both the disk and DB paths hand it the same
    dict — a second source of truth could only diverge from the first.
    """

    __tablename__ = "chart_specs"
    __table_args__ = {"schema": SCHEMA}

    # Bare filename (e.g. ec_a1b2c3d4e5f6.json), which is all the answer's fence and the
    # /charts/{filename} route ever carry.
    name = mapped_column(Text, primary_key=True)
    spec = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ReportRow(Base):
    """One generated session report, frozen.

    Both representations are kept on purpose. `payload` is the structured content, so a
    later, better template can produce a *new* version from it; `html` is what was rendered
    at generation time, so an existing version cannot change under a reader who has already
    been sent the link. Without the stored HTML, improving the template would silently
    restyle every report ever shared — which is what versioning exists to prevent.

    No PDF is stored: the PDF is the browser's print of this HTML, which is also what puts
    the charts in it (the server has no JS runtime).
    """

    __tablename__ = "reports"
    __table_args__ = (
        # Two concurrent generations must not land on the same version number.
        UniqueConstraint("session_id", "version", name="uq_reports_session_version"),
        {"schema": SCHEMA},
    )

    id = mapped_column(Text, primary_key=True)
    session_id = mapped_column(Text, ForeignKey(f"{SCHEMA}.sessions.id"), nullable=False, index=True)
    version = mapped_column(Integer, nullable=False)
    payload = mapped_column(JSONB, nullable=False)
    html = mapped_column(Text, nullable=False)
    #: How much of the session this version covered — shown in the document, and what makes
    #: "why does this report differ from the app?" answerable.
    message_count = mapped_column(Integer, nullable=False, default=0)
    created_by = mapped_column(Text)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class FeedbackRow(Base):
    __tablename__ = "feedback"
    __table_args__ = {"schema": SCHEMA}

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id = mapped_column(Text, index=True)
    trace_id = mapped_column(Text)
    level = mapped_column(String(16))
    rater_email = mapped_column(Text)
    rating = mapped_column(Text)
    reason = mapped_column(Text)
    comment = mapped_column(Text)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
