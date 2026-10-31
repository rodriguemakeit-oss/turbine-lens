/** The brand lockup: the mark, the constant brand word, and the product name.
 *
 *  There are two products on this brand — Turbine Lens Renewable Fleet Analyst and Turbine Lens
 *  Cost Forecast — so "Turbine Lens" is the constant and the product name is the variable.
 *  Giving them one typographic treatment would read as a single long title; the two levels
 *  are what make it read as "brand, then which product".
 *
 *  The two levels differ by weight, case, tracking and colour, all within one family —
 *  deliberately NOT by loading a second typeface. `Inter` is already named in
 *  tailwind.config.ts and never actually loaded (no @font-face, no stylesheet), so the app
 *  renders in system-ui today; a display face would need egress to a font CDN, which is the
 *  same corporate-network block that forces services/chart_assets.py to vendor ECharts. It
 *  would fail by silently falling back — leaving the two levels identical again, which is
 *  the one outcome this component exists to prevent.
 */
export function ProductLockup({ product, size = "sm" }: Readonly<{ product: string; size?: "sm" | "lg" }>) {
  const lg = size === "lg";
  return (
    <span className="flex items-center gap-2.5">
      {/* Decorative: the brand word is right beside it, so alt text would be read twice. */}
      <img
        src="/turbine_lens_mark.svg"
        alt=""
        aria-hidden
        className={lg ? "w-10 h-10" : "w-7 h-7"}
      />
      <span className="block leading-tight">
        <span className={lg ? "block font-bold text-lg text-gray-900" : "block font-bold text-sm text-gray-900"}>
          Turbine Lens
        </span>
        <span className="block text-xs uppercase tracking-[0.14em] text-gray-400 font-medium">
          {product}
        </span>
      </span>
    </span>
  );
}
