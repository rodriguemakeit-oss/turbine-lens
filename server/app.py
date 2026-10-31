"""Turbine Lens FastAPI application.

Serves the React frontend (client/out/) in production and exposes
all API endpoints under /api/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

_app_root = Path(__file__).parent.parent


from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from .db.database import bootstrap, is_configured, sweep_orphaned_executions
from .tracing import bootstrap as bootstrap_tracing
from .otel_tracing import bootstrap as bootstrap_otel_tracing
from .routers import (
    agent_router,
    charts_router,
    config_router,
    conversations_router,
    cost_router,
    feedback_router,
    reports_router,
    stats_router,
)
from .services import chart_assets, chart_backfill

logger = logging.getLogger(__name__)

#: Held so the vendoring task is not garbage-collected mid-download.
_vendor_task = None
#: Same, for the opt-in chart backfill.
_backfill_task = None

env = os.getenv("ENV", "production")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    logger.info("Starting Turbine Lens server...")

    # Bootstrap DB if configured
    if is_configured():
        logger.info("Initializing Lakebase persistence...")
        try:
            bootstrap()
            logger.info("Database ready.")
            # Any execution still marked 'running' predates this process, so it
            # was orphaned by a restart. Left alone it makes a reloaded page wait
            # forever on a turn that will never emit another event.
            try:
                swept = sweep_orphaned_executions()
                if swept:
                    logger.warning("Marked %d execution(s) orphaned by a restart as interrupted", swept)
            except Exception:
                logger.exception("Orphaned-execution sweep failed")
        except Exception as e:
            # Persistence stays unhealthy: is_healthy() reports false and the UI
            # surfaces it, rather than silently dropping every session.
            logger.error("Database bootstrap FAILED: %s. Persistence is degraded.", e)
    else:
        logger.warning("Lakebase not configured — persistence disabled.")

    # Bootstrap OpenTelemetry tracing tables if configured
    bootstrap_otel_tracing()

    # Bootstrap mlflow tracing tables if configured
    bootstrap_tracing()

    # Vendor the chart libraries and map GeoJSON in the background: four downloads
    # with generous timeouts must not sit in front of the Apps startup probe. Until
    # they land, chart HTML falls back to the CDN, so nothing is broken meanwhile.
    global _vendor_task
    _vendor_task = asyncio.create_task(asyncio.to_thread(chart_assets.vendor))

    # Opt-in one-shot recovery of charts drawn before specs were mirrored to Lakebase.
    # Inert unless TURBINE_LENS_CHART_BACKFILL is set, and in the background for the same
    # reason as the vendoring above — it makes one HTTP call per traced answer.
    global _backfill_task
    if chart_backfill.mode():
        _backfill_task = asyncio.create_task(chart_backfill.run())

    yield

    logger.info("Shutting down Turbine Lens server.")


class SafeJSONResponse(JSONResponse):
    """JSON responses that survive unpaired surrogates.

    A streamed emoji can be split across two chunks, leaving a lone surrogate in
    the accumulated text. Starlette's default renderer uses `ensure_ascii=False`
    and then `.encode("utf-8")`, which raises UnicodeEncodeError on such a
    character and turns the whole response into a 500 — so a single awkward byte
    boundary in an answer could make a session unreadable. Escaping non-ASCII
    instead is what the SSE path already does (plain `json.dumps`).
    """

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


app = FastAPI(
    title="Turbine Lens",
    description="Renewable-energy fleet analytics assistant",
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s %s: %s", request.method, request.url, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )


# CORS (dev only — production serves from same origin)
allowed_origins = ["http://localhost:3000", "http://localhost:5173"] if env == "development" else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routers
API_PREFIX = "/api"
app.include_router(config_router, prefix=f"{API_PREFIX}/config", tags=["config"])
app.include_router(conversations_router, prefix=API_PREFIX, tags=["conversations"])
app.include_router(agent_router, prefix=API_PREFIX, tags=["agent"])
app.include_router(feedback_router, prefix=API_PREFIX, tags=["feedback"])
app.include_router(reports_router, prefix=API_PREFIX, tags=["reports"])
app.include_router(cost_router, prefix=f"{API_PREFIX}/cost", tags=["cost"])
app.include_router(stats_router, prefix=f"{API_PREFIX}/stats", tags=["stats"])

# Charts — renders JSON (from agent tools) to HTML on the fly
app.include_router(charts_router, prefix="/charts", tags=["charts"])

# Serve React build (production)
_possible_build_paths = [
    _app_root / "client" / "out",
    Path(".") / "client" / "out",
    Path("/app/python/source_code") / "client" / "out",
]

build_path = None
for path in _possible_build_paths:
    if path.exists():
        build_path = path
        break

if build_path:
    logger.info("Serving static files from %s", build_path)
    index_html = build_path / "index.html"

    @app.exception_handler(StarletteHTTPException)
    async def spa_fallback(request: Request, exc: StarletteHTTPException):
        # Client-side routing means an unknown *page* path must serve index.html.
        # /api and /charts are excluded: they have real 404s, and returning the SPA
        # instead made a missing chart render the whole app inside its own iframe.
        if exc.status_code == 404 and not request.url.path.startswith(("/api", "/charts")):
            return FileResponse(index_html)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.mount("/", StaticFiles(directory=str(build_path), html=True), name="static")
else:
    logger.warning(
        f"Build directory not found in: {[str(p) for p in _possible_build_paths]}. "
        "In development, run Vite separately: cd client && npm run dev"
    )
