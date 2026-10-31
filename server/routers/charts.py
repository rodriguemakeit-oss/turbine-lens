"""Chart rendering endpoint.

Reads Plotly/ECharts JSON written by the agent tools and serves rendered HTML,
embedded by the client as an iframe. The charting libraries and the map GeoJSON are
served from this app rather than a public CDN — see services/chart_assets.py for
why that matters on a corporate network.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ..services import chart_assets, chart_render, chart_store

logger = logging.getLogger(__name__)
router = APIRouter()

_CHART_JSON_DIR = os.environ.get("TURBINE_LENS_CHART_DIR", "/tmp/turbine_lens_charts")


@router.get("/assets/{name}")
async def get_asset(name: str):
    """Serve a vendored chart library (echarts.min.js / plotly.min.js)."""
    path = chart_assets.lib_path(Path(name).name)
    if not path:
        raise HTTPException(status_code=404, detail="Asset not vendored")
    return FileResponse(path, media_type="application/javascript")


@router.get("/maps/{name}.json")
async def get_map(name: str):
    """Serve vendored GeoJSON for echarts.registerMap()."""
    path = chart_assets.map_path(Path(name).name)
    if not path:
        raise HTTPException(status_code=404, detail="Map not vendored")
    return FileResponse(path, media_type="application/json")


def _warm_cache(json_path: str, data: dict) -> None:
    """Put a chart recovered from Lakebase back on disk. Best-effort.

    Reopening an old conversation asks for every chart in it at once; without this each
    view would go back to the database for specs that cannot change.
    """
    try:
        os.makedirs(_CHART_JSON_DIR, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        logger.warning("could not re-cache chart %s", json_path, exc_info=True)


@router.get("/{filename}")
async def get_chart(filename: str):
    """Serve a chart as rendered HTML from its JSON source.

    Disk first, Lakebase as the durable fallback — see services/chart_store.py.
    """
    # Security: only allow bare filenames, no path traversal
    safe = Path(filename).name
    if ".." in safe or "/" in safe:
        raise HTTPException(status_code=400, detail="Invalid filename")

    json_name = safe.replace(".html", ".json")
    json_path = os.path.join(_CHART_JSON_DIR, json_name)

    if os.path.isfile(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Chart parse error: {e}") from e
    else:
        # The file lives on the container's disk, so every chart in a conversation older
        # than the current container is missing here. Lakebase holds the durable copy.
        data = await chart_store.load(json_name)
        if data is None:
            raise HTTPException(status_code=404, detail="Chart not found")
        _warm_cache(json_path, data)

    # The page is composed from the same pieces the report uses, so the palette and the
    # registerMap-before-setOption rule live in one place — see services/chart_render.py.
    return HTMLResponse(content=chart_render.page_html(json_name, data))
