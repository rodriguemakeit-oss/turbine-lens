import type React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Info } from "lucide-react";
import { toast } from "sonner";
import { api, type CostCurvePoint, type CostForecast, type CostRates, type Provenanced } from "@/lib/api";
import { CostChart } from "@/components/CostChart";
import { CapacityUnits } from "@/components/CapacityUnits";
import { ProductLockup } from "@/components/ProductLockup";
import { ThemeToggle } from "@/components/ThemeToggle";
import { UiBadge, UiCheckbox, UiFormItem, UiSegmentedControl, UiSegmentedControlButton, UiSlider } from "@/components/ui";
import type { BadgeVariant as UiBadgeVariant } from "@/components/ui";

/** Where a number came from. Shown next to every figure: without this, a step at 40 users
 *  reads as a measurement when it was a default somebody picked.
 *
 *  PREMIER COMPOSANT DU DESIGN SYSTEM employé dans l'app. Il a été choisi pour
 *  commencer parce qu'il cumulait à lui seul trois écarts à la charte (arrondi
 *  `rounded` au lieu de la pilule, `text-[10px]` sous le plancher, `font-medium`
 *  qui n'existe pas) et qu'il ne dépend d'aucune icône.
 *
 *  Ce qu'on cesse de maintenir en le basculant : la table de teintes maison
 *  ci-dessous devenait une seconde description des couleurs de statut, à tenir à
 *  jour en parallèle du système. `variant` la remplace.
 *
 *  Vérifié avant écriture, dans le CSS compilé et les déclarations : `UiBadge`
 *  accepte bien ses enfants (contrairement à `UiTag`, qui ignore les siens et ne
 *  lit que `label`), il rend un `<p>` mais en `display: inline-flex` — donc la
 *  mise en ligne à côté du chiffre tient. */
function SourceBadge({ p }: Readonly<{ p?: Provenanced }>) {
  if (!p) return null;
  const label: Record<string, string> = {
    measured: "measured",
    list_price: "list price",
    published: "published",
    config: "config",
    assumption: "assumption",
    unknown: "not set",
  };
  /** Les variantes de la charte remplacent les teintes maison. Valeurs admises
   *  relevées dans `BADGE_VARIANTS` : neutral, danger, warning, success,
   *  information, discovery, planet, ai. */
  const variant: Record<string, UiBadgeVariant> = {
    measured: "success",
    list_price: "success",
    published: "information",
    config: "information",
    assumption: "warning",
    unknown: "danger",
  };
  return (
    <UiBadge
      className="ml-2"
      variant={variant[p.source] ?? "neutral"}
      emphasis="subtle"
      scale="sm"
      isUppercase
      title={[p.note, p.as_of && `as of ${p.as_of}`].filter(Boolean).join(" · ")}
    >
      {label[p.source] || p.source}
    </UiBadge>
  );
}

function money(usd: number | null | undefined) {
  if (usd === null || usd === undefined || !Number.isFinite(usd)) return "—";
  return usd.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function NumberField({
  label, value, onChange, min = 0, step = 1, suffix, hint,
}: Readonly<{
  label: string; value: number; onChange: (v: number) => void;
  min?: number; step?: number; suffix?: string; hint?: string;
}>) {
  /* `id` est OBLIGATOIRE sur UiFormItem — c'est lui qui associe le label au
     champ. Sans identifiant stable, deux champs de même libellé entreraient en
     collision ; on le dérive donc du libellé. */
  const id = `nf-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="block">
      <span className="flex items-center gap-1.5">
        {/* UiFormItem lit `e.target.value` de façon STANDARD, contrairement à
            UiInputSearch et UiCheckbox qui passent la valeur en 2e argument. La
            convention n'est pas uniforme dans la bibliothèque : à vérifier
            composant par composant. */}
        <UiFormItem
          id={id}
          name={id}
          type="number"
          label={label}
          labelKind="static"
          value={String(value)}
          min={min}
          step={step}
          /* La cible de l'événement est typée `HTMLElement` et non
             `HTMLInputElement` : `e.target.value` ne compile pas tel quel. Le
             transtypage est donc obligatoire — c'est le troisième piège annoncé
             pour ce composant, et le seul des trois que TypeScript signale. */
          onChange={(e) =>
            onChange(Math.max(min, Number((e.target as HTMLInputElement).value) || 0))
          }
          className="w-full"
        />
        {suffix && <span className="text-xs text-gray-400 whitespace-nowrap">{suffix}</span>}
      </span>
      {hint && <span className="block text-xs text-gray-400 mt-0.5">{hint}</span>}
    </div>
  );
}

function SliderField({
  label, value, onChange, max, hint,
}: Readonly<{ label: string; value: number; onChange: (v: number) => void; max: number; hint?: string }>) {
  const id = `sf-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="block">
      {/* Le curseur porte lui-même son libellé ; le champ numérique à côté sert à
          saisir une valeur exacte, ce que le curseur ne permet pas au pixel près.
          Les deux pilotent le même état, d'où le libellé masqué sur le second :
          le répéter le ferait annoncer deux fois par un lecteur d'écran. */}
      <UiSlider
        id={id}
        name={id}
        label={label}
        min={0}
        max={max}
        value={Math.min(value, max)}
        step={1}
        /* Pas de `hasTooltip` : elle afficherait la valeur en surimpression, alors
           que le champ « Exact value » juste en dessous la montre déjà et qu'ils
           sont liés au même état. Elle chevauchait en plus le libellé du curseur. */
        /* QUATRIÈME convention `onChange` de cette bibliothèque, sur cinq
           composants employés : UiSlider reçoit DIRECTEMENT la valeur numérique,
           sans événement. (UiInputSearch et UiCheckbox passent (e, valeur) ;
           UiFormItem passe un événement standard ; ici, un simple `number`.)
           `step` est par ailleurs obligatoire, contrairement à un <input range>. */
        onChange={(valeur) => onChange(valeur ?? 0)}
      />
      {/* Sur sa PROPRE ligne, et non serré à droite du curseur : UiFormItem
          impose sa largeur minimale — mesuré à 210px alors que `w-28` en demande
          112 — et débordait du panneau. Même comportement que UiSegmentedControl
          avec `className` : la classe passée ne contraint pas la mise en forme du
          composant. Autant lui laisser la place plutôt que de lutter. */}
      <div className="mt-1">
        <UiFormItem
          id={`${id}-exact`}
          name={`${id}-exact`}
          type="number"
          label="Exact value"
          labelKind="static"
          value={String(value)}
          min={0}
          onChange={(e) =>
            onChange(Math.max(0, Number((e.target as HTMLInputElement).value) || 0))
          }
        />
      </div>
      {hint && <span className="block text-xs text-gray-400">{hint}</span>}
    </div>
  );
}

/** Round adoption levels people actually argue about, so the conversation starts somewhere
 *  concrete instead of at whatever the inputs happened to be left on. */
const PRESETS: { label: string; users: number; concurrent: number }[] = [
  { label: "Today (9)", users: 9, concurrent: 2 },
  { label: "Team (50)", users: 50, concurrent: 5 },
  { label: "Department (100)", users: 100, concurrent: 10 },
  { label: "Fleet-wide (200)", users: 200, concurrent: 20 },
];

const RATE_LABELS: Record<string, string> = {
  // Named by what they cover, not by their contract letter: "Discount B" means nothing to
  // anyone reading the page, and the grouping is the part that gets questioned.
  discount_a_pct: "Discount A — Apps + SQL warehouse (% off list)",
  discount_b_pct: "Discount B — Genie + Lakebase (% off list)",
  usd_per_user_month: "LLM $ per user / month",
  genie_dbu_per_user_month: "Genie DBU per user / month",
  usd_per_dbu_genie: "$ per DBU — Genie",
  usd_per_dbu_sql: "$ per DBU — serverless SQL",
  dbu_per_hr_sql: "DBU per hour — warehouse size",
  usd_per_dbu_apps: "$ per DBU — Apps",
  dbu_per_hr_apps: "DBU per hour — app compute size",
  usd_per_cu_hr_lakebase: "$ per CU-hour — Lakebase",
  turns_per_user_month: "Turns per user / month (reference only)",
};

const ASSUMPTION_LABELS: Record<string, [string, string]> = {
  working_hours_per_day: ["Working hours per day", "Hours the service must be reachable. 24 = as deployed today"],
  peak_hours_per_day: ["Peak hours per day", "The busiest window inside the working day"],
  working_days_per_month: ["Working days per month", "Also scales LLM and Genie — 0 days means no turns"],
  sql_hours_per_day: ["SQL warehouse awake hours / day",
    "Measured 6.2: auto-stop already keeps it asleep the rest of the time"],
  users_per_instance: ["Concurrent users per app instance", "Assumption — needs a load test to confirm"],
  users_per_cu: ["Concurrent users per Lakebase CU", "Assumption"],
  queries_in_flight_per_user: ["SQL queries in flight per user", "Includes Genie's generated SQL"],
  queries_per_cluster: ["Queries per SQL cluster", "Published: one cluster per 10 concurrent queries"],
  in_hours_instances: ["App instances during working hours", "The floor that keeps it reachable outside the peak"],
  in_hours_cu: ["Lakebase CU during working hours", "The floor that keeps Postgres reachable"],
  min_instances: ["App instances outside working hours", "0 = stopped overnight and at weekends"],
  min_cu: ["Lakebase CU outside working hours", "0 = Postgres stopped overnight"],
};

export function CostPage(): React.ReactElement {
  const [rates, setRates] = useState<CostRates | null>(null);
  const [users, setUsers] = useState(100);
  const [concurrent, setConcurrent] = useState(20);
  const [assumptions, setAssumptions] = useState<Record<string, number>>({});
  const [obo, setObo] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, number>>({});
  const [result, setResult] = useState<CostForecast | null>(null);
  const [showAssumptions, setShowAssumptions] = useState(false);
  const [curve, setCurve] = useState<CostCurvePoint[]>([]);
  const [scaleConcurrency, setScaleConcurrency] = useState(true);
  const [axisMax, setAxisMax] = useState(100);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    api.getCostRates()
      .then((r) => {
        setRates(r);
        setAssumptions(r.assumption_defaults);
      })
      .catch(() => toast.error("Could not load rates"));
  }, []);

  const run = useCallback(() => {
    const shared = {
      users,
      concurrent_users: concurrent,
      assumptions: { ...assumptions, genie_on_behalf_of_user: obo },
      rate_overrides: overrides,
    };
    api.forecastCost(shared)
      .then(setResult)
      .catch((e) => toast.error(e instanceof Error ? e.message : "Forecast failed"));
    // The curve carries the concurrency ratio implied by the two inputs across the axis,
    // so changing either input reshapes the whole line rather than sliding a dot along it.
    api.costCurve({ ...shared, users_max: axisMax, points: 26, scale_concurrency: scaleConcurrency })
      .then((c) => setCurve(c.series))
      .catch(() => setCurve([]));
  }, [users, concurrent, assumptions, obo, overrides, axisMax, scaleConcurrency]);

  // Debounced: the arithmetic lives on the server so every input change is a round trip,
  // and typing "100" should not fire three forecasts.
  useEffect(() => {
    if (!rates) return;
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(run, 250);
    return () => { if (timer.current) window.clearTimeout(timer.current); };
  }, [rates, run]);

  const missing = result?.missing_rates ?? rates?.missing ?? [];
  const perUser = result?.usd_per_user_month;

  const lines = useMemo(() => result?.lines ?? [], [result]);

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-surface border-b border-gray-200 px-6 py-3 flex items-center gap-3">
        <a href="#/" className="text-gray-400 hover:text-turbine_lens-primary transition" title="Back to chat">
          <ArrowLeft size={18} />
        </a>
        <h1>
          <ProductLockup product="Cost Forecast" />
          {/* The full name for anything that reads the document rather than looks at it —
              a screen reader, a browser tab restore, a search index. */}
          <span className="sr-only">Turbine Lens Cost Forecast</span>
        </h1>
        <span className="text-xs text-gray-400 border-l border-gray-200 pl-3">
          What it costs per month at a given adoption level
        </span>
        {/* Poussé à droite de l'en-tête : cet écran n'a rien dans ce coin. */}
        <div className="ml-auto">
          <ThemeToggle />
        </div>
      </header>

      <div className="max-w-6xl mx-auto p-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Inputs */}
        <div className="lg:col-span-1 space-y-4">
          <section className="bg-surface rounded-button border border-gray-200 p-4 space-y-4">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-turbine_lens-primary">
              Adoption
            </h2>
            {/* Choix exclusif : c'est un contrôle segmenté, écrit à la main pour
                la troisième fois dans cette app. `value` est dérivé du couple
                (users, concurrent) ; aucun préréglage actif donne une chaîne vide,
                donc aucun segment marqué — ce qui est le bon rendu quand
                l'utilisateur a saisi ses propres valeurs. */}
            <UiSegmentedControl
              value={PRESETS.find((x) => x.users === users && x.concurrent === concurrent)?.label ?? ""}
              onChange={(valeur) => {
                const choisi = PRESETS.find((x) => x.label === valeur);
                if (choisi) { setUsers(choisi.users); setConcurrent(choisi.concurrent); }
              }}
              scale="sm"
              aria-label="Niveau d'adoption"
            >
              {PRESETS.map((x) => (
                <UiSegmentedControlButton key={x.label} value={x.label}>
                  {x.label}
                </UiSegmentedControlButton>
              ))}
            </UiSegmentedControl>
            <SliderField label="Total active users" value={users} onChange={setUsers}
              max={axisMax} hint="Drives the consumption lines (LLM, Genie)" />
            <SliderField label="Peak concurrent users" value={concurrent} onChange={setConcurrent}
              max={Math.max(10, Math.ceil(axisMax / 2))}
              hint="Drives the capacity lines (SQL, Apps, Lakebase)" />
            <p className="text-xs text-gray-500">
              {users > 0
                ? `Peak concurrency is ${((concurrent / users) * 100).toFixed(0)}% of active users.`
                : "Set a user count to derive the concurrency ratio."}
            </p>
            <button type="button"
              onClick={() => setAssumptions((a) => ({
                ...a, working_hours_per_day: 8, min_instances: 0, min_cu: 0,
              }))}
              className="w-full text-left text-xs px-2 py-1.5 rounded-button border border-gray-300 text-gray-600 hover:border-turbine_lens-primary hover:text-turbine_lens-primary transition"
              title="Sets working hours to 8 and stops the app and Postgres outside them"
            >
              Try: stop the app + Postgres outside working hours
            </button>

            {/* `children` fonctionne sur UiCheckbox — vérifié au banc — alors que
                UiButton les ignore. Le piège est propre au composant, pas général.
                On garde donc le libellé riche tel quel. `subscriptMessage` aurait
                été l'autre voie, mais il rend en VERT : une explication neutre s'y
                lirait comme un message de succès. */}
            <UiCheckbox
              checked={!scaleConcurrency}
              onChange={(_e, checked) => setScaleConcurrency(checked !== true)}
            >
              <span className="text-xs text-gray-600">
                Pin concurrency across the chart{" "}
                <span className="block text-gray-400">
                  Off (default): the ratio above is carried along the x axis, so capacity steps
                  appear. On: concurrency stays fixed — "adoption grows but peak load doesn't".
                </span>
              </span>
            </UiCheckbox>
          </section>

          <section className="bg-surface rounded-button border border-gray-200 p-4">
            <button type="button"
              onClick={() => setShowAssumptions((v) => !v)}
              className="w-full text-left text-xs font-semibold uppercase tracking-wide text-turbine_lens-primary"
            >
              Assumptions {showAssumptions ? "▾" : "▸"}
            </button>
            {showAssumptions && (
              <div className="mt-3 space-y-3">
                {Object.keys(ASSUMPTION_LABELS).map((k) => (
                  <NumberField
                    key={k}
                    label={ASSUMPTION_LABELS[k][0]}
                    hint={ASSUMPTION_LABELS[k][1]}
                    value={assumptions[k] ?? 0}
                    step={0.5}
                    onChange={(v) => setAssumptions((a) => ({ ...a, [k]: v }))}
                  />
                ))}
              </div>
            )}
          </section>

          <section className="bg-surface rounded-button border border-gray-200 p-4">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-turbine_lens-primary mb-2">
              Rates
            </h2>
            <p className="text-xs text-gray-500 mb-3">
              Read from <code className="text-xs">system.billing</code> when the app has the
              grant. Anything marked <em>not set</em> can be typed in — it will be labelled as
              your figure, never as a list price.
            </p>
            <div className="space-y-3">
              {Object.keys(RATE_LABELS).map((k) => {
                const p = (result?.rates ?? rates?.rates)?.[k];
                const live = p?.value;
                return (
                  <label key={k} className="block">
                    <span className="text-xs font-medium text-gray-600">
                      {RATE_LABELS[k]}
                      <SourceBadge p={p} />
                    </span>
                    <input
                      type="number"
                      step="0.001"
                      min={0}
                      placeholder={live === null || live === undefined ? "not set" : String(live)}
                      value={overrides[k] ?? (live ?? "")}
                      onChange={(e) => {
                        const v = e.target.value;
                        setOverrides((o) => {
                          const next = { ...o };
                          if (v === "") delete next[k];
                          else next[k] = Number(v);
                          return next;
                        });
                      }}
                      className="mt-1 w-full rounded-button border border-gray-300 px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-turbine_lens-primary"
                    />
                  </label>
                );
              })}
            </div>
          </section>
        </div>

        {/* Output */}
        <div className="lg:col-span-2 space-y-4">
          <section className="bg-surface rounded-button border border-gray-200 p-5">
            <p className="text-xs text-gray-500">Estimated monthly cost</p>
            {result?.ready ? (
              <>
                <p className="text-4xl font-semibold text-gray-900 mt-1">
                  {money(result.total_usd_month)}
                </p>
                <p className="text-xs text-gray-500 mt-1">
                  {perUser != null && <>{money(perUser)} per active user / month</>}
                  {result.effective_discount_pct > 0 && (
                    <>
                      {perUser != null && " · "}
                      after portfolio discounts ({result.discounts.a}% Apps/SQL ·{" "}
                      {result.discounts.b}% Genie/Lakebase · none on the LLM ={" "}
                      {result.effective_discount_pct.toFixed(1)}% effective), from{" "}
                      {money(result.total_usd_month_list)} list
                    </>
                  )}
                </p>
              </>
            ) : (
              <>
                <p className="text-4xl font-semibold text-gray-300 mt-1">pending</p>
                <p className="text-xs text-red-600 mt-1">
                  {missing.length} rate{missing.length === 1 ? "" : "s"} not set — a total would be
                  meaningless. The unit counts and breakpoints below are unaffected.
                </p>
              </>
            )}
          </section>

          <section className="bg-surface rounded-button border border-gray-200 p-4">
            <div className="flex items-baseline justify-between mb-1">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-turbine_lens-primary">
                Monthly cost by adoption level
              </h2>
              <span className="flex items-center gap-2 text-xs text-gray-400">
                x-axis max
                <UiSegmentedControl
                  value={String(axisMax)}
                  onChange={(valeur) => setAxisMax(Number(valeur))}
                  scale="sm"
                  aria-label="Maximum de l'axe des abscisses"
                >
                  {[50, 100, 200].map((m) => (
                    <UiSegmentedControlButton key={m} value={String(m)}>
                      {String(m)}
                    </UiSegmentedControlButton>
                  ))}
                </UiSegmentedControl>
              </span>
            </div>
            <CostChart series={curve} currentUsers={users} ready={Boolean(result?.ready)} />
            <p className="text-xs text-gray-400 mt-1">
              Stacked by component. The flat left edge is the always-on floor — one app
              instance and one Lakebase capacity unit cost the same at zero users.
            </p>
          </section>

          {result && (
            <CapacityUnits lines={result.lines} breakpoints={result.breakpoints}
              ready={result.ready} />
          )}

          <section className="bg-surface rounded-button border border-gray-200 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="text-left px-4 py-2">Component</th>
                  <th className="text-left px-4 py-2">Driver</th>
                  <th className="text-left px-4 py-2">How</th>
                  <th className="text-right px-4 py-2">$ / month</th>
                </tr>
              </thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.key} className="border-t border-gray-100 align-top">
                    <td className="px-4 py-2.5 font-medium text-gray-900">{l.label}</td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">
                      {l.driver === "users" ? "total users" : "concurrent"}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">
                      {l.formula}
                      {l.detail.peak_units !== undefined && (
                        <span className="block text-gray-400 mt-0.5">
                          {String(l.detail.peak_units)} {String(l.detail.unit_name)}
                          {Number(l.detail.peak_units) === 1 ? "" : "s"} at peak ·
                          baseline {String(l.detail.baseline_units)}
                        </span>
                      )}
                      {/* Per line, because the discount is per line: the LLM exemption is
                          the whole point of the change and belongs on the page, not only
                          in the model. */}
                      <span className="block text-gray-400 mt-0.5">
                        {l.discount_pct > 0
                          ? `−${l.discount_pct}% portfolio discount`
                          : "no negotiated discount — billed at list"}
                        {result?.ready && l.discount_pct > 0 && ` · ${money(l.usd_list)} list`}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                      {result?.ready ? money(l.usd) : <span className="text-gray-300">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="bg-surface rounded-button border border-gray-200 p-4">
            <UiCheckbox checked={obo} onChange={(_e, checked) => setObo(checked === true)}>
              <span className="text-sm">
                Genie called on behalf of each user{" "}
                <span className="block text-xs text-amber-700">
                  Applies the 150 DBU/user/month free allowance. <strong>Not how the app works
                  today</strong> — it calls Genie as the service principal, which gets no
                  allowance. Potentially the largest single saving in this model.
                </span>
              </span>
            </UiCheckbox>
          </section>

          {result && result.notes.length > 0 && (
            <section className="bg-amber-50 border border-amber-200 rounded-button p-4">
              <h2 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-800 mb-2">
                <Info size={13} /> Read this with the number
              </h2>
              <ul className="space-y-1.5 list-disc list-inside">
                {result.notes.map((n) => (
                  <li key={n} className="text-[12px] text-amber-900">{n}</li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
