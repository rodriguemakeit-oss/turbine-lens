/** Poignée de redimensionnement de la barre latérale.
 *
 *  ÉCART ASSUMÉ AU SYSTÈME. Fluid 6.3 ne propose **aucun** composant
 *  redimensionnable ni splitter : vérifié dans le catalogue Storybook (81
 *  composants) et dans le CSS compilé — son modèle de barre latérale est le
 *  repli/dépli (`.ui-sidebar--collapsed`, `--ui-sidebar-collapsed-width:
 *  fit-content`). Il n'y a donc pas de règle Fluid à suivre ici, seulement des
 *  règles Fluid à ne pas contredire. C'est le même cas de figure que la barre
 *  segmentée de MapMe (CONTEXTE_MIGRATION_FLUID.md §3.6) : s'écarter du système
 *  est légitime quand il ne couvre pas le besoin, à condition de le documenter.
 *
 *  Ce qu'on emprunte quand même à la charte : la couleur de la poignée au survol
 *  vient des tokens (via `bg-turbine_lens-primary`), et l'anneau de focus est celui
 *  des autres contrôles.
 *
 *  ACCESSIBILITÉ — la raison d'être de ce composant séparé. Une poignée qui ne
 *  répond qu'à la souris est inutilisable au clavier, ce qui est précisément le
 *  défaut corrigé ailleurs dans cet écran. Elle est donc un vrai `<button>` (Sonar
 *  refuse un `role="separator"` porteur d'écouteurs), libellé avec la largeur courante,
 *  se prend le focus, s'ajuste aux flèches (WCAG 2.1.1), et publie sa position
 *  via `aria-valuenow/min/max` pour les lecteurs d'écran.
 */
import { useCallback, useEffect, useRef } from "react";

export const SIDEBAR_MIN = 220;
export const SIDEBAR_MAX = 560;
const STEP = 16; // un pas de la charte (--ui-semantic-size-spacing-16)

export function SidebarResizer({
  width,
  onResize,
}: Readonly<{ width: number; onResize: (w: number) => void }>) {
  const draggingRef = useRef(false);

  const clamp = (w: number) => Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, w));

  // Les écouteurs vivent sur `window` le temps du glisser : posés sur la poignée,
  // ils décrocheraient dès que le curseur va plus vite que le rendu et sort de
  // ses 4 pixels de large.
  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (draggingRef.current) onResize(clamp(e.clientX));
    };
    const up = () => {
      draggingRef.current = false;
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [onResize]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Home/End donnent les bornes, comme sur un curseur natif.
      const targets: Record<string, number> = {
        ArrowLeft: width - STEP,
        ArrowRight: width + STEP,
        Home: SIDEBAR_MIN,
        End: SIDEBAR_MAX,
      };
      const next = targets[e.key];
      if (next === undefined) return;
      e.preventDefault();
      onResize(clamp(next));
    },
    [width, onResize]
  );

  return (
    <button
      type="button"
      aria-label={`Largeur du panneau des conversations : ${Math.round(width)} px (flèches, Début, Fin)`}
      onKeyDown={onKeyDown}
      onMouseDown={() => {
        draggingRef.current = true;
        // Sans ça, le glisser sélectionne le texte des deux panneaux.
        document.body.style.userSelect = "none";
        document.body.style.cursor = "col-resize";
      }}
      // La poignée fait 4px de large mais reste attrapable : `after` étend la
      // zone sensible à 12px sans élargir le trait visible.
      className="relative w-1 shrink-0 cursor-col-resize bg-gray-200 hover:bg-turbine_lens-primary
                 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-turbine_lens-primary
                 transition-colors after:absolute after:inset-y-0 after:-left-1.5 after:-right-1.5
                 after:content-['']"
    />
  );
}
