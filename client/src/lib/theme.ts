/** Accès aux tokens de la charte depuis du JavaScript.
 *
 *  Les graphiques (ECharts) n'acceptent pas de `var(--ui-*)` : il leur faut une
 *  couleur résolue. On lit donc la valeur calculée au moment du rendu, ce qui a
 *  deux vertus — les couleurs viennent de la charte, et elles suivent le thème,
 *  puisqu'un changement de `data-theme` change ce que renvoie getComputedStyle.
 *
 *  Sans ça, les graphiques restaient sur des hex en dur : fond blanc et encre
 *  sombre, donc illisibles dès que l'interface passait en sombre.
 */

/** Résout un token de la charte en couleur utilisable. Le repli n'est là que pour
 *  le cas où la feuille de tokens n'a pas encore été appliquée. */
export function njColor(token: string, fallback = "#000000"): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.body).getPropertyValue(token).trim();
  return v || fallback;
}

/** Palette catégorielle pour les séries de graphiques, prise dans les familles
 *  d'accent de la charte. Ordre fixe : une série garde sa teinte quel que soit
 *  son rang, sinon un filtre repeindrait les survivantes. */
export const CHART_SERIES_TOKENS = [
  "--ui-semantic-color-background-accent-blue-solid-default",
  "--ui-semantic-color-background-accent-teal-solid-default",
  "--ui-semantic-color-background-accent-orange-solid-default",
  "--ui-semantic-color-background-accent-purple-solid-default",
  "--ui-semantic-color-background-accent-green-solid-default",
] as const;

/** Les rôles non catégoriels d'un graphique : surface, encres, filets. */
export const chartInk = () => ({
  surface: njColor("--ui-semantic-color-background-neutral-primary-default", "#ffffff"),
  ink: njColor("--ui-semantic-color-text-neutral-primary-default", "#171d21"),
  inkMuted: njColor("--ui-semantic-color-text-neutral-tertiary-default", "#60798b"),
  hairline: njColor("--ui-semantic-color-border-neutral-subtle-default", "#ccd4da"),
});

/** Prévient quand le thème change, pour re-rendre ce qui a figé des couleurs.
 *  Couvre la bascule manuelle (attribut `data-theme`) ET le mode "system", où
 *  c'est l'OS qui décide sans que l'attribut bouge. */
export function onThemeChange(cb: () => void): () => void {
  const obs = new MutationObserver(cb);
  obs.observe(document.body, { attributes: true, attributeFilter: ["data-theme"] });
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", cb);
  return () => {
    obs.disconnect();
    mq.removeEventListener("change", cb);
  };
}
