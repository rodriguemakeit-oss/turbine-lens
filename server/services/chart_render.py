"""Chart HTML, in pieces, so one chart and many charts share the same renderer.

`/charts/{name}.html` serves one chart as a standalone page for the chat's iframes. A
session report needs several charts on **one** page — and inlined rather than iframed,
because an iframe that has not finished loading prints blank and a fixed-height frame
clips across a page break.

Rather than a second copy of the house style and the map-registration dance, the page is
split into `deps_html()` (once) plus `block_html()` (per chart). Both callers compose from
the same pieces, so the palette, the fonts and the "registerMap before setOption" rule can
only be defined once. That rule is load-bearing: ECharts v5 ships no map data, so a map
chart drawn before its GeoJSON is registered renders blank.
"""

from __future__ import annotations

import json

from . import chart_assets

_COLORWAY = [
    "#1ABFA6",
    "#3B82F6",
    "#F59E0B",
    "#EF4444",
    "#8B5CF6",
    "#10B981",
    "#6366F1",
    "#EC4899",
    "#14B8A6",
    "#F97316",
]

#: Merged BEFORE the model's option (setOption merges), so every chart gets the Turbine Lens
#: palette, fonts and motion without the model having to ask for them.
ECHARTS_BASE = {
    "color": _COLORWAY,
    "textStyle": {"fontFamily": "Inter, system-ui, -apple-system, sans-serif", "color": "#1F2937"},
    "animation": True,
    "animationDuration": 800,
    # confine: an iframe clips anything drawn past its edge, so a tooltip near the
    # border was cut off.
    "tooltip": {"trigger": "item", "axisPointer": {"type": "shadow"}, "confine": True},
    "grid": {"left": "3%", "right": "4%", "bottom": "3%", "top": 56, "containLabel": True},
}


def _js(value) -> str:
    """JSON for embedding inside a <script> block.

    `json.dumps` does not escape `<`, so a spec containing the literal `</script>` would
    close the tag and inject markup — and these specs come from the model. Escaping the
    angle bracket keeps the payload valid JSON while making that impossible.
    """
    return json.dumps(value).replace("<", "\\u003c")


def is_echarts(name: str, spec: dict) -> bool:
    """Which library draws this spec — by filename prefix, then by shape."""
    return name.startswith("ec_") or ("series" in spec and "data" not in spec)


def uses_map(option: dict) -> bool:
    """True if the option renders on a geo/map coordinate system."""
    if "geo" in option:
        return True
    series = option.get("series")
    if isinstance(series, dict):
        series = [series]
    elif not isinstance(series, list):
        series = []
    return any(isinstance(s, dict) and (s.get("type") == "map" or s.get("coordinateSystem") == "geo") for s in series)


import re

_CARTESIAN = {"line", "bar", "scatter", "effectScatter", "candlestick", "boxplot", "heatmap"}
_SLIDER_ROOM = 72        # px kept under the plot area for the horizontal slider
_RANK_WINDOW = 15        # categories shown at once on a long horizontal ranking
_SLIDER_MIN_POINTS = 20  # below this, wheel zoom + toolbox are enough
_H_MIN, _H_MAX, _H_DEFAULT = 280, 1200, 480
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _as_list(v) -> list:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return [v] if isinstance(v, dict) else []


def _n_points(axis: dict, series: list) -> int:
    data = axis.get("data")
    if isinstance(data, list) and data:
        return len(data)
    return max((len(s.get("data") or []) for s in series), default=0)


def interactive_echarts(option: dict) -> dict:
    """Navigation for a chat chart (zoom, slider, toolbox), never overriding the model.

    Chat only: the printed report must stay static, so `block_html` applies this only
    when `interactive=True`.
    """
    opt = dict(option)
    series = _as_list(opt.get("series"))
    xs, ys = _as_list(opt.get("xAxis")), _as_list(opt.get("yAxis"))
    cartesian = bool(xs and ys) and any(s.get("type") in _CARTESIAN for s in series)

    features: dict = {"saveAsImage": {"pixelRatio": 2, "title": "PNG"}}
    if cartesian:
        features = {"dataZoom": {"yAxisIndex": "none"}, "restore": {}, **features}
    opt.setdefault("toolbox", {"right": 12, "top": 4, "itemSize": 14, "feature": features})

    # A long legend wraps onto the plot, or runs off the edge; scrolling keeps it on one line.
    if isinstance(opt.get("legend"), dict):
        opt["legend"] = {"type": "scroll", **opt["legend"]}

    if cartesian and "tooltip" not in opt and any(s.get("type") in {"line", "bar"} for s in series):
        opt["tooltip"] = {"trigger": "axis", "confine": True, "axisPointer": {"type": "line"}}

    if not cartesian or "dataZoom" in opt:  # the model's own dataZoom wins
        return opt

    grid = dict(opt["grid"]) if isinstance(opt.get("grid"), dict) else {}
    ranking = ys[0].get("type") == "category" and xs[0].get("type") != "category"

    if ranking:
        n = _n_points(ys[0], series)
        if n > _RANK_WINDOW:
            span = round(100 * _RANK_WINDOW / n)
            # A non-inverted category axis draws its first item at the bottom, so
            # show the top of the list.
            start, end = (0, span) if ys[0].get("inverse") else (100 - span, 100)
            opt["dataZoom"] = [
                {"type": "inside", "yAxisIndex": 0, "start": start, "end": end,
                 "zoomOnMouseWheel": False, "moveOnMouseWheel": True},
                {"type": "slider", "yAxisIndex": 0, "right": 8, "width": 16,
                 "start": start, "end": end, "brushSelect": False},
            ]
            grid.setdefault("right", 44)
    else:
        idx = list(range(len(xs)))
        zooms = [{"type": "inside", "xAxisIndex": idx, "filterMode": "none"}]
        if _n_points(xs[0], series) > _SLIDER_MIN_POINTS:
            zooms.append({"type": "slider", "xAxisIndex": idx, "bottom": 12, "height": 22,
                          "filterMode": "none"})
            grid.setdefault("bottom", _SLIDER_ROOM)
        opt["dataZoom"] = zooms

    if grid:
        opt["grid"] = grid
    return opt


def _looks_like_dates(traces) -> bool:
    for t in traces or []:
        x = t.get("x") if isinstance(t, dict) else None
        if isinstance(x, list) and x:
            return isinstance(x[0], str) and bool(_DATE_RE.match(x[0]))
    return False


def interactive_plotly(fig: dict) -> dict:
    """Same intent for Plotly: fill the frame, keep labels inside, slider on time axes."""
    fig = dict(fig)
    layout = dict(fig.get("layout") or {})
    # A fixed height or width set by the model is what overflowed the iframe. The
    # height is already passed on through height_hint(); here the chart fills its frame.
    layout.pop("height", None)
    layout.pop("width", None)
    layout["autosize"] = True
    for ax in ("xaxis", "yaxis"):
        layout[ax] = {"automargin": True, **(layout.get(ax) or {})}
    if layout["xaxis"].get("type") == "date" or _looks_like_dates(fig.get("data")):
        layout["xaxis"].setdefault("rangeslider", {"visible": True, "thickness": 0.08})
    traces = fig.get("data") or []
    if all(isinstance(t, dict) and t.get("type", "scatter") in {"scatter", "bar"} for t in traces):
        layout.setdefault("hovermode", "x unified")
    fig["layout"] = layout
    return fig


def height_hint(spec: dict) -> int:
    """How tall the chart wants to be in the chat; the iframe grows to this height."""
    h = spec.get("_height")
    if h is None and isinstance(spec.get("layout"), dict):
        h = spec["layout"].get("height")
    try:
        if h is not None:
            return max(_H_MIN, min(_H_MAX, int(h)))
    except (TypeError, ValueError):
        pass
    ys = _as_list(spec.get("yAxis"))
    if ys and ys[0].get("type") == "category":  # horizontal ranking: one row per category
        n = len(ys[0].get("data") or [])
        return max(_H_DEFAULT, min(_H_MAX, min(n, _RANK_WINDOW) * 28 + 160))
    return _H_DEFAULT

def deps_html(*, echarts: bool = True, plotly: bool = False, animate: bool = True) -> str:
    """Library tags plus the shared base option and map registration. Emit once per page.

    Map GeoJSON is fetched once into a shared promise every block waits on, so a page with
    six map charts does not fetch the same country six times. The `.catch` keeps a missing
    map degrading to an empty canvas rather than to a page that never draws anything.

    `animate=False` for print: a chart captured mid-animation prints half-drawn.
    """
    base = dict(ECHARTS_BASE)
    if not animate:
        base["animation"] = False

    out = []
    if plotly:
        out.append(f'<script src="{chart_assets.lib_src("plotly.min.js")}"></script>')
    if not echarts:
        # Nothing below applies to Plotly, and the map loop would reference `echarts`.
        return "\n".join(out)

    out.append(f'<script src="{chart_assets.lib_src("echarts.min.js")}"></script>')
    maps = chart_assets.available_maps()
    out.append(
        "<script>\n"
        f"window.__fmBase = {_js(base)};\n"
        f"window.__fmMaps = Promise.all({_js(maps)}.map(function(m) {{\n"
        "  return fetch('/charts/maps/' + m + '.json')\n"
        "    .then(function(r) { return r.json(); })\n"
        "    .then(function(g) { echarts.registerMap(m, g); });\n"
        "})).catch(function() {});\n"
        "</script>"
    )
    return "\n".join(out)

def block_html(dom_id: str, spec: dict, *, name: str = "", height: str = "420px",
               renderer: str = "canvas", interactive: bool = False) -> str:
    spec = dict(spec)
    hinted = spec.pop("_height", None)
    if hinted and height != "100vh":
        height = f"{int(hinted)}px" if str(hinted).isdigit() else height

    if not is_echarts(name, spec):
        if interactive:
            spec = interactive_plotly(spec)
        config = {"responsive": True}
        if interactive:
            config.update({"displaylogo": False, "scrollZoom": True,
                           "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                           "toImageButtonOptions": {"format": "png", "scale": 2}})
        return (
            f'<div id="{dom_id}" style="width:100%;height:{height}"></div>\n'
            f"<script>(function() {{ var fig = {_js(spec)};\n"
            f"Plotly.newPlot('{dom_id}', fig.data || [], fig.layout || {{}}, {_js(config)}); }})();</script>"
        )

    if interactive:
        spec = interactive_echarts(spec)
    return (
        f'<div id="{dom_id}" style="width:100%;height:{height}"></div>\n'
        "<script>(function() {\n"
        f"  var el = document.getElementById('{dom_id}');\n"
        f"  var c = echarts.init(el, null, {{renderer:'{renderer}'}});\n"
        f"  var option = {_js(spec)};\n"
        "  function draw() { c.setOption(window.__fmBase); c.setOption(option); }\n"
        "  (window.__fmMaps || Promise.resolve()).then(draw).catch(draw);\n"
        # ResizeObserver follows the frame as it is dragged or goes full screen, which
        # window 'resize' does not always do. The 'resize' listener stays as a fallback.
        "  if (window.ResizeObserver) { new ResizeObserver(function() { c.resize(); }).observe(el); }\n"
        "  window.addEventListener('resize', function() { c.resize(); });\n"
        "})();</script>"
    )


def page_html(name: str, spec: dict) -> str:
    """One chart as a standalone page, the one the chat's iframes load.

    The page tells the parent how tall it wants to be (postMessage). Without this the
    iframe stayed at a fixed 450 px and any `_height` was lost.
    """
    echarts = is_echarts(name, spec)
    hint = height_hint(spec)  # before block_html pops `_height`
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">\n'
        + deps_html(echarts=echarts, plotly=not echarts)
        + "\n<style>html,body{margin:0;padding:0;background:#fff;height:100%;overflow:hidden}</style>\n"
        "</head><body>\n"
        + block_html("c", spec, name=name, height="100vh", interactive=True)
        + "\n<script>try{parent.postMessage({type:'fm-chart-height',height:"
        + str(int(hint)) + "}, location.origin);}catch(e){}</script>"
        + "\n</body></html>"
    )