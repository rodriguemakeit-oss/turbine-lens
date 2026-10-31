import type React from "react";
import { njColor } from "@/lib/theme";
import { Boxes, Database, Server } from "lucide-react";
import type { CostBreakpoint, CostLine } from "@/lib/api";
import { cn } from "@/lib/utils";

/** One glyph per provisioned thing — a unit chart. Crossing a capacity threshold literally
 *  adds a glyph, which is the whole point: the step is a thing you can see coming.
 *
 *  Icons come from lucide (already a dependency, already the app's visual language) rather
 *  than emoji: emoji cannot carry the component's colour, and they render differently on
 *  every OS. Colours match the chart's fixed component mapping so the two panels agree. */
/** Each component keeps its own vocabulary. A warehouse has no "overnight" band at all —
 *  auto-stop decides that, there is nothing to choose — so saying "none overnight" for it
 *  described a setting that does not exist. And a band whose count is zero still has to be
 *  named, or the hours look like they were never considered. */
/** Libellé d'une bande horaire : nommée même à zéro unité, mais uniquement si la
 *  fenêtre existe. Extrait des ternaires imbriqués d'origine, à logique identique. */
function bandLabel(windowHours: number, units: number, noun: string, zeroLabel: string | null): string | null {
  if (windowHours <= 0) return null;
  return units > 0 ? `${units} ${noun}` : zeroLabel;
}

type Kind = {
  icon: React.ElementType;
  /** Token de la charte, résolu au rendu : les hex en dur ne suivaient pas le thème. */
  colorToken: string;
  noun: string;
  /** Units held while the service is up but outside the peak. */
  inHours: string;
  inHoursZero: string;
  /** Null when the component has no outside-working-hours band. */
  off: string | null;
  offZero: string | null;
};

const KINDS: Record<string, Kind> = {
  sql: {
    icon: Boxes, colorToken: "--ui-semantic-color-background-accent-orange-solid-default", noun: "cluster",
    inHours: "awake outside the peak", inHoursZero: "asleep outside the peak",
    off: null, offZero: null,
  },
  apps: {
    icon: Server, colorToken: "--ui-semantic-color-background-accent-red-solid-default", noun: "instance",
    inHours: "in working hours", inHoursZero: "stopped outside the peak",
    off: "around the clock", offZero: "none outside working hours",
  },
  lakebase: {
    icon: Database, colorToken: "--ui-semantic-color-background-accent-purple-solid-default", noun: "capacity unit",
    inHours: "in working hours", inHoursZero: "stopped outside the peak",
    off: "around the clock", offZero: "none outside working hours",
  },
};

/** Beyond this many glyphs a row stops being readable and starts being wallpaper. */
const MAX_GLYPHS = 24;

function money(v: number) {
  return `$${Math.round(v).toLocaleString("en-US")}`;
}

function ordinal(n: number) {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

export function CapacityUnits({
  lines, breakpoints, ready,
}: Readonly<{ lines: CostLine[]; breakpoints: CostBreakpoint[]; ready: boolean }>) {
  const rows = lines.filter((l) => KINDS[l.key]);
  if (rows.length === 0) return null;

  return (
    <section className="bg-surface rounded-button border border-gray-200 p-4">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-turbine_lens-primary">
          Provisioned capacity
        </h2>
        {/* Stated because it is otherwise surprising: dragging total users moves nothing here. */}
        <span className="text-xs text-gray-400">sized by peak concurrency</span>
      </div>

      <div className="space-y-4">
        {rows.map((line) => {
          const kind = KINDS[line.key];
          const Icon = kind.icon;
          const peak = Number(line.detail.peak_units ?? 0);
          // Three bands, three visual states. Reading a single "baseline" was the bug:
          // with off-hours units at 0 and the in-hours floor at 1 it still claimed one
          // unit was held around the clock.
          const inHours = Number(line.detail.in_hours_units ?? 0);
          const offHours = Number(line.detail.off_units ?? 0);
          const perHour = Number(line.detail.unit_usd_per_hr ?? 0);
          const peakHours = Number(line.detail.peak_hours_month ?? 0);
          const inHoursHours = Number(line.detail.in_hours_hours ?? 0);
          const offHoursHours = Number(line.detail.off_hours ?? 0);

          const cost = {
            off: perHour * (peakHours + inHoursHours + offHoursHours),
            inHours: perHour * (peakHours + inHoursHours),
            peak: perHour * peakHours,
          };

          const total = Math.max(peak, inHours, offHours);
          const shown = Math.min(total, MAX_GLYPHS);
          const hidden = total - shown;
          const next = breakpoints.find((b) => b.key === line.key);

          const band = (i: number) => {
            if (i < offHours) return "off";
            if (i < inHours) return "inHours";
            return "peak";
          };
          const STYLE = {
            off: { fill: "33", dashed: false, dot: true },
            inHours: { fill: "1a", dashed: false, dot: false },
            peak: { fill: "00", dashed: true, dot: false },
          } as const;
          const WHAT = {
            off: (c: string) => `Around the clock: ${Math.round(peakHours + inHoursHours + offHoursHours)} h/month = ${c}/mo`,
            inHours: (c: string) => `Working hours only: ${Math.round(peakHours + inHoursHours)} h/month = ${c}/mo`,
            peak: (c: string) => `Peak only: ${Math.round(peakHours)} h/month = ${c}/mo`,
          } as const;

          return (
            <div key={line.key}>
              <div className="flex items-baseline justify-between">
                <span className="text-xs font-medium text-gray-700">{line.label}</span>
                <span className="text-xs text-gray-500 font-mono tabular-nums">
                  {[
                    `${peak} at peak`,
                    // Name the band even at zero, but only when such a window exists.
                    bandLabel(inHoursHours, inHours, kind.inHours, kind.inHoursZero),
                    kind.off ? bandLabel(1, offHours, kind.off, kind.offZero) : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </div>

              <div className="flex flex-wrap items-center gap-2 mt-2">
                {Array.from({ length: shown }).map((_, i) => {
                  const b = band(i);
                  const st = STYLE[b];
                  return (
                    <span
                      key={`${line.key}-${b}-${i}`}
                      title={WHAT[b](money(cost[b]))}
                      className={cn(
                        "relative inline-flex items-center justify-center w-11 h-11 rounded-button border-2",
                        st.dashed ? "border-dashed" : "border-transparent"
                      )}
                      style={(() => {
                        // Résolu ici (et non en constante) pour suivre le thème.
                        const c = njColor(kind.colorToken, "#60798b");
                        return {
                          color: c,
                          backgroundColor: st.fill === "00" ? "transparent" : `${c}${st.fill}`,
                          borderColor: st.dashed ? `${c}66` : "transparent",
                        };
                      })()}
                    >
                      <Icon size={24} strokeWidth={1.75} />
                      {st.dot && (
                        <span
                          className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full ring-2 ring-surface"
                          style={{ backgroundColor: njColor(kind.colorToken, "#60798b") }}
                          aria-hidden
                        />
                      )}
                    </span>
                  );
                })}

                {hidden > 0 && (
                  <span className="text-xs text-gray-400 ml-1">+{hidden} more</span>
                )}

                {/* The ghost: the next unit, before you need it. */}
                {next && (
                  <span className="flex items-center gap-1.5 ml-2">
                    <span
                      className="inline-flex items-center justify-center w-11 h-11 rounded-button border-2 border-dashed opacity-40"
                      style={{ color: njColor("--ui-semantic-color-text-neutral-tertiary-default"), borderColor: njColor("--ui-semantic-color-border-neutral-subtle-default") }}
                      title="Not provisioned yet"
                    >
                      <Icon size={24} strokeWidth={1.75} />
                    </span>
                    <span className="text-xs text-gray-500">
                      {ordinal(next.units_after)} at{" "}
                      <strong className="text-gray-700">{next.at_concurrent_users}</strong>{" "}
                      concurrent
                      {ready && ` · +${money(next.usd_step)}/mo`}
                    </span>
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <p className="text-xs text-gray-400 mt-3">
        Solid with a dot = held around the clock · solid = held through working hours ·
        dashed = added only at peak. Hover any unit for what it costs on its own: the same
        unit costs several times more around the clock than at peak only, purely because of
        how many hours it is held.
      </p>
    </section>
  );
}
