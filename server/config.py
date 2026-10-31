"""Environment-based configuration. Defaults are examples, not live resource IDs."""

from __future__ import annotations

import os

# --- LLM (CLI path) -------------------------------------------------------
MODEL = os.environ.get("model", "databricks-claude-opus-4-6")
GATEWAY = os.environ.get("TURBINE_LENS_GATEWAY", "")

# --- Catalogs -------------------------------------------------------------
# READ_CATALOG is a Delta Sharing catalog, so it is reachable only from
# workspaces it has been bound to, and its name may differ per environment.
READ_CATALOG = os.environ.get("TURBINE_LENS_READ_CATALOG", "turbine_lens_telemetry_data")
# The validated energy-loss view (invariant #4) is NOT in the project share yet,
# so it keeps its own catalog. Two catalogs is the lesser evil: the share's
# nearest table, stoplogbook.mismatch_wind_10min, carries the validation inputs
# and NO loss column, so repointing invariant #4 at it would not fail loudly —
# it would quietly answer a different question. Fold this back into READ_CATALOG
# when the planned CDC copy lands in the turbine_lens catalog.
LOSS_CATALOG = os.environ.get("TURBINE_LENS_LOSS_CATALOG", "turbine_lens_dev")
# Written by the separate ingest bundle. Reached through Genie only — none of
# its schemas belong in ALLOWED_TABLES (invariant #5).
INGEST_CATALOG = os.environ.get("TURBINE_LENS_INGEST_CATALOG", "turbine_lens_dev")

# --- Genie spaces (from Turbine Lens_App) ------------------------------------
# Space IDs are workspace-scoped: the same space redeployed in another workspace
# gets a different ID, so these must come from the environment or the agent's
# whole qualitative surface goes silently dark in a new workspace.
GENIE_SPACES = {
    # Two qualitative-context spaces, deliberately named for their source system: REMA
    # (reported issues/events) and REX (Return of Experience — lessons learned). They
    # complement each other, so the agent must be able to tell them apart from the tool
    # name alone; `qualitative_context` on its own could not.
    "rema_qualitative_context": os.environ.get(
        "TURBINE_LENS_GENIE_REMA", "00000000000000000000000000000000"
    ),
    "rex_qualitative_context": os.environ.get(
        "TURBINE_LENS_GENIE_REX", "00000000000000000000000000000000"
    ),
    # Lorinlog event log. Reads a *view* (<ingest catalog>.lorinlogs.lorinlog_events_genie)
    # over the table data/jobs/lorinlog_excel_to_delta.ipynb builds — business columns
    # only, description as plain text. Don't point anything at the base table expecting
    # the same shape.
    "lorin_qualitative_context": os.environ.get(
        "TURBINE_LENS_GENIE_LORIN", "00000000000000000000000000000000"
    ),
    "anomaly_detection": os.environ.get(
        "TURBINE_LENS_GENIE_ANOMALY", "00000000000000000000000000000000"
    ),
    "alarm_stop_correlation": os.environ.get(
        "TURBINE_LENS_GENIE_ALARM_STOP", "00000000000000000000000000000000"
    ),
    "fleet_reporting": os.environ.get(
        "TURBINE_LENS_GENIE_FLEET_REPORTING", "00000000000000000000000000000000"
    ),
}
GENIE_QUERY_TIMEOUT_SECONDS = 180
GENIE_MAX_ATTEMPTS = 2
GENIE_MAX_RESULT_ROWS = 500
GENIE_AGENT_POLL_INTERVAL_SECONDS = float(os.environ.get("GENIE_POLL_INTERVAL", "0.5"))

# --- Read-data compute ----------------------------------------------------
SQL_WAREHOUSE_ID = os.environ.get("TURBINE_LENS_WAREHOUSE_ID", "1000000000000000")

# --- Table allowlist (read-only) ------------------------------------------
# Built from READ_CATALOG so the allowlist follows the environment. The SCHEMA
# and TABLE parts are literals on purpose: the allowlist is a security boundary
# (invariant #5), and making its shape configurable would let an env var widen
# what the agent may read.
ALLOWED_TABLES = [
    # The CDS telemetry. NB the project share is the one place this table is not
    # called cds_data.cds_10min_wind — verified column-identical (22 of 22) to
    # the name it had in the hackathon share, so only the path moved.
    f"{READ_CATALOG}.cds.wind_10min",
    f"{READ_CATALOG}.theoretical_active_power.wind_10min",
    f"{READ_CATALOG}.pic.wind_turbine",
    # The fleet hierarchy: every farm of every technology (wind, solar, BESS, hybrid),
    # including the 137 wind farms with no turbines recorded, rolled up from one row per
    # substation. It is the fallback tier behind `pic.wind_turbine` in `resolve_site`,
    # and it is allowlisted so the agent can also answer the org questions it carries
    # (which regional office or subsidiary owns a farm). Reference data only — no
    # measurements, and NO grid-connection state, which is why `active` is inferred.
    f"{READ_CATALOG}.pic.hierarchy",
    # The validated energy-loss view (invariant #4): a stop is recorded only if
    # stoplogbook-logged AND algorithm-validated. Energy loss comes from nowhere else.
    # LOSS_CATALOG, not READ_CATALOG — see the note there.
    f"{LOSS_CATALOG}.telemetry.wind_stops",
]
# NB : la théorique du fallback (turbine_lens-stop-loss) utilise
# `theoretical_active_power.wind_10min` (déjà ci-dessus). NE PAS ajouter
# `ml_score.wind_10min_theoretical_active_power` : déprécié / retiré du share
# (Vincent, 2026-08-07).

# --- Phase flags -----------------------------------------------------------
PHASE0 = os.environ.get("TURBINE_LENS_PHASE0", "true").lower() == "true"

# --- Genie mode (default) --------------------------------------------------
# "agent" = new Agent Mode API (SSE + items), "classic" = legacy conversation API
# Per-session overrides are stored in the database; this is the global fallback.
GENIE_MODE_DEFAULT: str = os.environ.get("TURBINE_LENS_GENIE_MODE", "classic")

# --- Allow edit tools in agent loop (default) ---------------------------------------------
ALLOW_EDIT = os.environ.get("ALLOW_EDIT", "true").lower() == "true"


def get_genie_mode() -> str:
    """Global default genie mode (per-session overrides live in persistence)."""
    return GENIE_MODE_DEFAULT


def get_workspace_host() -> str:
    """Workspace URL injected into the Databricks App runtime."""
    return os.environ.get("DATABRICKS_HOST", "").rstrip("/")
