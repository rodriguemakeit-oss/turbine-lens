/** Sélecteur de thème — clair / système / sombre.
 *
 *  La charte Fluid embarque les deux thèmes dans la même feuille de tokens ; la
 *  bascule se fait par l'attribut `data-theme` sur <body> ("system" suit l'OS).
 *  Le choix est mémorisé localement : sans ça, l'app repartirait sur le réglage
 *  du système à chaque chargement, ce qui donne l'impression que le bouton ne
 *  sert à rien.
 */
import { useEffect, useState } from "react";
import { UiSegmentedControl, UiSegmentedControlButton } from "@/components/ui";

type Theme = "light" | "system" | "dark";
const KEY = "turbine_lens.theme";

/** Icônes Material, celles qu'attend le design system — plus de lucide ici. */
const OPTIONS: { value: Theme; label: string; icone: string }[] = [
  { value: "light", label: "Thème clair", icone: "light_mode" },
  { value: "system", label: "Thème du système", icone: "desktop_windows" },
  { value: "dark", label: "Thème sombre", icone: "dark_mode" },
];

function read(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* stockage indisponible (navigation privée stricte) : on suit l'OS */
  }
  return "system";
}

export function ThemeToggle({ className = "" }: Readonly<{ className?: string }>) {
  const [theme, setTheme] = useState<Theme>(read);

  useEffect(() => {
    document.body.dataset.theme = theme;
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* non bloquant : le thème reste appliqué pour la session en cours */
    }
  }, [theme]);

  return (
    /* Le geste était bon, l'écriture ne l'était pas : ce sélecteur reproduisait à
       la main un contrôle segmenté (groupe de boutons exclusifs, un seul actif)
       que la charte fournit — `UiSegmentedControl`. On garde donc exactement le
       même comportement, mais l'apparence, les états de survol, de focus et de
       sélection viennent désormais du système au lieu d'être maintenus ici. */
    <UiSegmentedControl
      value={theme}
      onChange={(valeur) => setTheme(valeur as Theme)}
      scale="sm"
      aria-label="Thème de l'interface"
      /* PIÈGE : `UiSegmentedControl` REMPLACE sa classe de base par celle qu'on
         lui passe, il ne la fusionne pas. Mesuré : avec `className=""` (la valeur
         par défaut de ce composant), le conteneur sortait avec `class=""` — la
         classe `nj-segmented-control` disparaissait, donc le contrôle perdait
         tout son habillage, sans la moindre erreur. On ne transmet donc la classe
         que si elle est non vide. */
      {...(className ? { className } : {})}
    >
      {OPTIONS.map(({ value, label, icone }) => (
        <UiSegmentedControlButton
          key={value}
          value={value}
          iconName={icone}
          aria-label={label}
          title={label}
        />
      ))}
    </UiSegmentedControl>
  );
}
