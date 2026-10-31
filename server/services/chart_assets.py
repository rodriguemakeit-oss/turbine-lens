"""Vendored chart libraries + map GeoJSON, fetched once at startup.

Charts are iframes whose HTML this server generates, so something has to supply
the charting library — and, for maps, the GeoJSON, because ECharts v5 ships no map
data at all and you must `registerMap()` it yourself.

Two ways to supply it: point the browser at a public CDN, or fetch it once here and
serve it from the app. We do the latter deliberately. The browser is a corporate
desktop that may not reach jsdelivr or plot.ly, whereas this container already talks
to Databricks APIs — so moving the dependency server-side turns "every chart is
blank for some users" into "one download at boot". The Gradio implementation made
the same call, in its own words: "there is NO external CDN dependency that a
locked-down App network could block."

The CDN stays as a fallback, so a container *without* egress still renders plain
charts for users who do have it. If both are blocked the chart is blank either way,
and nothing is lost by trying.

Versions are pinned rather than floating (`plotly-latest`) so a chart cannot change
appearance, or break, because an upstream release shipped.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_CHART_DIR = os.environ.get("TURBINE_LENS_CHART_DIR", "/tmp/turbine_lens_charts")
ASSET_DIR = os.path.join(_CHART_DIR, "assets")
MAP_DIR = os.path.join(_CHART_DIR, "maps")

#: filename -> (CDN url, minimum plausible size in bytes)
_LIBS = {
    "echarts.min.js": ("https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js", 100_000),
    "plotly.min.js": ("https://cdn.plot.ly/plotly-2.35.2.min.js", 500_000),
}

#: ECharts v5 ships no maps. "world" allows turbine/farm scatter by [lng, lat];
#: "france" allows a choropleth by French region.
_MAPS = {
    "world": "https://cdn.jsdelivr.net/npm/echarts@4.9.0/map/json/world.json",
    "france": (
        "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/regions-version-simplifiee.geojson"
    ),
}

_vendored_libs: set[str] = set()
_vendored_maps: list[str] = []


def _fetch(url: str, timeout: int = 20) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return r.read()


def vendor() -> None:
    """Download libraries and maps. Best-effort; safe to call more than once."""
    os.makedirs(ASSET_DIR, exist_ok=True)
    os.makedirs(MAP_DIR, exist_ok=True)

    for name, (url, min_size) in _LIBS.items():
        path = os.path.join(ASSET_DIR, name)
        if name in _vendored_libs and os.path.isfile(path):
            continue
        try:
            data = _fetch(url)
            if len(data) < min_size:
                raise ValueError(f"suspiciously small ({len(data)} bytes)")
            with open(path, "wb") as fh:
                fh.write(data)
            _vendored_libs.add(name)
            logger.info("vendored %s (%d bytes)", name, len(data))
        except Exception:
            logger.warning("could not vendor %s — falling back to CDN", name, exc_info=True)

    for name, url in _MAPS.items():
        if name in _vendored_maps:
            continue
        try:
            data = _fetch(url)
            json.loads(data)  # reject an error page masquerading as GeoJSON
            with open(os.path.join(MAP_DIR, f"{name}.json"), "wb") as fh:
                fh.write(data)
            _vendored_maps.append(name)
            logger.info("vendored ECharts map '%s' (%d bytes)", name, len(data))
        except Exception:
            logger.warning("could not vendor map '%s'", name, exc_info=True)

    if not _vendored_maps:
        logger.warning(
            "No maps vendored — map charts will render blank. Note that "
            "skills/turbine_lens-graph/SKILL.md advertises 'world' and 'france' to the "
            "agent, so it will keep producing them."
        )


def lib_src(name: str) -> str:
    """URL for a chart library: the vendored copy if we have it, else the CDN."""
    if name in _vendored_libs:
        return f"/charts/assets/{name}"
    return _LIBS[name][0]


def lib_path(name: str) -> str | None:
    path = os.path.join(ASSET_DIR, name)
    return path if name in _LIBS and os.path.isfile(path) else None


def available_maps() -> list[str]:
    return list(_vendored_maps)


def map_path(name: str) -> str | None:
    path = os.path.join(MAP_DIR, f"{os.path.basename(name)}.json")
    return path if os.path.isfile(path) else None
