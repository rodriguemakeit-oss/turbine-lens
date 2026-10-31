import { useEffect, useRef, useState } from "react";
import type { CostCurvePoint } from "@/lib/api";
import { CHART_SERIES_TOKENS, chartInk, njColor, onThemeChange } from "@/lib/theme";

/** Turbine Lens's house categorical hues, in fixed order, assigned to components by identity
 *  and never by size — a filter or a change in ranking must not repaint the survivors.
 *
 *  Validated for this use (light surface, adjacent pairs): worst CVD ΔE 13.9 deutan /
 *  10.6 tritan, worst normal-vision ΔE 19.8 — all above the floors. Two hues sit under
 *  3:1 contrast against white, which is why the numeric table below the chart is not
 *  optional: it is the required fallback reading. */
const SERIES: { key: string; label: string }[] = [
  { key: "llm", label: "Agent LLM" },
  { key: "genie", label: "Genie" },
  { key: "sql", label: "SQL warehouse" },
  { key: "apps", label: "Apps compute" },
  { key: "lakebase", label: "Lakebase" },
];

const TOTAL_LABEL = "Total / month";
// Plus d'hex en dur : ces rôles viennent de la charte et sont relus à chaque
// rendu, sans quoi le graphique gardait un fond blanc et une encre sombre en
// thème sombre — illisible.

/** The vendored library, not a CDN — the corporate network may block public CDNs, which is
 *  the whole reason services/chart_assets.py exists. If vendoring failed the chart is simply
 *  absent and the table carries the numbers. */
let pending: Promise<unknown> | null = null;
export function loadECharts(): Promise<any> {
  const w = window as unknown as { echarts?: unknown };
  if (w.echarts) return Promise.resolve(w.echarts);
  pending ??= new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = "/charts/assets/echarts.min.js";
    el.onload = () => resolve((window as unknown as { echarts: unknown }).echarts);
    el.onerror = () => reject(new Error("echarts unavailable"));
    document.head.appendChild(el);
  });
  return pending as Promise<any>;
}

function usd(v: number) {
  return v >= 1000 ? `$${(v / 1000).toFixed(1)}k` : `$${Math.round(v)}`;
}

export function CostChart({
  series, currentUsers, ready,
}: Readonly<{ series: CostCurvePoint[]; currentUsers: number; ready: boolean }>) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<any>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let disposed = false;
    loadECharts()
      .then((echarts) => {
        if (disposed || !host.current) return;
        // SVG renderer: crisp at any zoom and in a print, unlike a canvas raster.
        chart.current = echarts.init(host.current, null, { renderer: "svg" });
      })
      .catch(() => setFailed(true));
    return () => {
      disposed = true;
      chart.current?.dispose();
      chart.current = null;
    };
  }, []);

  // Force un re-rendu quand le thème change : les couleurs sont figées dans
  // l'option ECharts au moment du setOption.
  const [themeTick, setThemeTick] = useState(0);
  useEffect(() => onThemeChange(() => setThemeTick((n) => n + 1)), []);

  useEffect(() => {
    if (!chart.current || series.length === 0) return;
    // Rôles de la charte, relus à chaque rendu (donc à chaque changement de thème).
    const { surface: SURFACE, ink: INK, inkMuted: INK_MUTED, hairline: HAIRLINE } = chartInk();
    const seriesColor = (i: number) => njColor(CHART_SERIES_TOKENS[i % CHART_SERIES_TOKENS.length]);
    const xs = series.map((p) => p.users);
    const current = series.reduce(
      (best, p) => (Math.abs(p.users - currentUsers) < Math.abs(best.users - currentUsers) ? p : best),
      series[0]
    );

    chart.current.setOption(
      {
        animationDuration: 300,
        // Room for the x-axis band inside the container, so the card never grows a
        // nested scrollbar to reach its own axis labels.
        grid: { left: 8, right: 20, top: 40, bottom: 8, containLabel: true },
        legend: {
          top: 0, left: 0, itemWidth: 10, itemHeight: 10, itemGap: 14,
          // Identity lives in the legend, never in a number stamped on every point.
          textStyle: { color: INK_MUTED, fontSize: 12 },
          data: [...SERIES.map((s) => s.label), TOTAL_LABEL],
        },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "cross", crossStyle: { color: HAIRLINE }, label: { show: false } },
          backgroundColor: SURFACE,  // themed like the ink: a white box under light ink was illegible in dark mode
          borderColor: HAIRLINE,
          textStyle: { color: INK, fontSize: 12 },
          formatter: (params: any[]) => {
            if (!params?.length) return "";
            const u = params[0].axisValue;
            const parts = params.filter((p) => p.seriesName !== TOTAL_LABEL);
            const totalSeries = params.find((p) => p.seriesName === TOTAL_LABEL);
            // Never sum `params` now: it contains the total series too, which would
            // double-count. Prefer the total series' own value.
            const total = totalSeries
              ? totalSeries.data?.[1] ?? 0
              : parts.reduce((sum, p) => sum + (p.data?.[1] ?? 0), 0);
            const rows = parts
              .slice()
              .reverse()
              .map(
                (p) =>
                  `<div style="display:flex;gap:8px;justify-content:space-between">
                     <span>${p.marker} ${p.seriesName}</span>
                     <span style="font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace">${usd(p.data?.[1] ?? 0)}</span>
                   </div>`
              )
              .join("");
            return `<div style="min-width:190px">
                      <div style="font-weight:600;margin-bottom:4px">${Math.round(u)} active users</div>
                      ${rows}
                      <div style="border-top:1px solid ${HAIRLINE};margin-top:5px;padding-top:4px;
                                  display:flex;justify-content:space-between;font-weight:600">
                        <span>Total / month</span>
                        <span style="font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace">${usd(total)}</span>
                      </div>
                    </div>`;
          },
        },
        xAxis: {
          type: "value",
          name: "Active users",
          nameLocation: "middle",
          nameGap: 26,
          nameTextStyle: { color: INK_MUTED, fontSize: 12 },
          min: 0,
          max: xs[xs.length - 1],
          // Solid hairlines only: a dashed grid reads as a threshold when it is just a grid.
          axisLine: { lineStyle: { color: HAIRLINE } },
          axisTick: { show: false },
          axisLabel: { color: INK_MUTED, fontSize: 12 },
          splitLine: { show: false },
        },
        yAxis: {
          type: "value",
          // One axis, always. Cost per user is a different scale and lives in a stat tile,
          // never on a second y-axis.
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: INK_MUTED, fontSize: 12, formatter: (v: number) => usd(v) },
          splitLine: { lineStyle: { color: HAIRLINE, type: "solid", width: 1 } },
        },
        series: SERIES.map((s, i) => ({
          name: s.label,
          type: "line",
          stack: "cost",
          symbol: "none",
          // A 2px line in the SURFACE colour is the gap between stacked fills — not a border
          // drawn around each mark, which is the anti-pattern.
          lineStyle: { width: 2, color: SURFACE },
          areaStyle: { color: seriesColor(i), opacity: 0.9 },
          emphasis: { focus: "series" },
          data: series.map((p) => [p.users, p.lines[s.key] ?? 0]),
        })).concat([
          {
            // The total is an aggregate, not a sixth workload, so it wears ink rather than
            // a categorical hue — a colour here would imply it is another component.
            // Unstacked, and last in the list so it draws on top of the bands.
            name: TOTAL_LABEL,
            type: "line",
            symbol: "none",
            lineStyle: { width: 2, color: INK },
            itemStyle: { color: INK },
            z: 10,
            data: series.map((p) => [p.users, p.total_usd_month]),
            markPoint: ready
              ? {
                  symbol: "circle",
                  symbolSize: 9,
                  itemStyle: { color: INK, borderColor: SURFACE, borderWidth: 2 },
                  label: {
                    formatter: `now · ${usd(current.total_usd_month)}`,
                    position: "top",
                    color: INK,
                    fontSize: 12,
                    fontWeight: 700,
                  },
                  data: [{ coord: [current.users, current.total_usd_month] }],
                }
              : undefined,
          } as any,
        ]),
      },
      { notMerge: true }
    );
  }, [series, currentUsers, ready, themeTick]);

  useEffect(() => {
    const onResize = () => chart.current?.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  if (failed) {
    return (
      <p className="text-xs text-gray-500 p-4">
        Chart unavailable (the charting library is not vendored on this container). The
        figures below are unaffected.
      </p>
    );
  }
  return <div ref={host} style={{ width: "100%", height: 340 }} />;
}
