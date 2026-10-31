import type { Config } from "tailwindcss";

/** Pointe une variable Fluid tout en gardant les modificateurs d'opacité Tailwind.
 *
 *  Une couleur déclarée simplement en `var(--ui-…)` casse **silencieusement** dès
 *  qu'on écrit `bg-x/30` : Tailwind ne sait pas injecter d'alpha dans une var().
 *  La forme `rgb(from … r g b / <alpha-value>)` restaure ce comportement — et le
 *  code en dépend réellement (`ring-turbine_lens-primary/30`, `bg-turbine_lens-primary/5`…).
 *  Voir CONTEXTE_MIGRATION_FLUID.md §4.
 */
const nj = (token: string) => `rgb(from var(${token}) r g b / <alpha-value>)`;

const config: Config = {
  /* `banc/` doit être analysé au même titre que `src/`. Tailwind n'émet que les
   * classes qu'il TROUVE dans ces chemins : une classe écrite uniquement dans le
   * banc ne serait jamais générée, et la page se rendrait sans elle — sans
   * erreur, sans avertissement. Le banc montrerait alors autre chose que
   * l'application, ce qui en ferait un instrument trompeur au lieu d'un contrôle.
   * Piège inactif au moment où cette ligne est écrite (le banc n'a pas encore de
   * classe qui lui soit propre, et BenchPage.tsx vit sous `src/`), mais il se
   * déclencherait au premier ajout. */
  content: ["./index.html", "./src/**/*.{ts,tsx}", "./banc/**/*.{ts,tsx,html}"],
  theme: {
    extend: {
      colors: {
        /* ————————————————————————————————————————————————————————————————
         * PASSERELLE 1 — les rôles hérités, rebranchés sur des tokens.
         *
         * Le code écrit `bg-turbine_lens-primary` à 55 endroits. Plutôt que de
         * reprendre les écrans, on redirige le rôle vers la couleur d'action de
         * la charte : Blue Bolt (#007BC5, blue-600) — la seule du nuancier qui
         * passe AA en interactif. L'ergonomie ne bouge pas, la couleur si.
         * À supprimer quand `grep -r turbine_lens-` ne renvoie plus rien.
         * ———————————————————————————————————————————————————————————————— */
        turbine_lens: {
          primary: nj("--ui-semantic-color-background-accent-blue-solid-default"),
          secondary: nj("--ui-semantic-color-background-accent-blue-secondary-default"),
          accent: nj("--ui-semantic-color-background-status-warning-solid-default"),
          danger: nj("--ui-semantic-color-background-status-danger-solid-default"),
          success: nj("--ui-semantic-color-background-status-success-solid-default"),
          purple: nj("--ui-semantic-color-background-status-discovery-solid-default"),
        },

        /* ————————————————————————————————————————————————————————————————
         * PASSERELLE 2 — l'échelle de gris (fond par défaut).
         *
         * Fluid nomme ses gris `grey`, le code hérité écrit `gray`. Cette échelle
         * ne sert que de FOND : le texte et les bordures sont redirigés plus bas
         * vers les tokens SÉMANTIQUES, seuls garants du contraste.
         * ———————————————————————————————————————————————————————————————— */
        gray: {
          50: nj("--ui-semantic-color-background-neutral-secondary-default"),
          100: nj("--ui-semantic-color-background-neutral-secondary-default"),
          200: nj("--ui-core-color-grey-200"),
          300: nj("--ui-core-color-grey-300"),
          400: nj("--ui-core-color-grey-400"),
          500: nj("--ui-core-color-grey-500"),
          600: nj("--ui-core-color-grey-600"),
          700: nj("--ui-core-color-grey-700"),
          800: nj("--ui-core-color-grey-800"),
          900: nj("--ui-semantic-color-background-neutral-inverse-default"),
        },

        /* ————————————————————————————————————————————————————————————————
         * PASSERELLE 3 — les couleurs de statut héritées de la palette Tailwind.
         *
         * Les écrans emploient encore ~30 classes `amber/red/green/blue/yellow`
         * (bandeaux d'alerte, pastilles, sélecteur de mode agent). Elles sont hors
         * charte ET ne suivent pas le thème sombre. On rebranche les familles
         * entières sur les tokens de statut Fluid, sans toucher une seule classe :
         * les fonds clairs (50/100/200) vont sur les variantes `tertiary`, les
         * teintes soutenues (500+) sur `solid`, et le texte sur `text-status-*`,
         * qui reste lisible dans les deux thèmes.
         * ———————————————————————————————————————————————————————————————— */
        red: {
          50: nj("--ui-semantic-color-background-status-danger-tertiary-default"),
          100: nj("--ui-semantic-color-background-status-danger-tertiary-default"),
          200: nj("--ui-semantic-color-background-status-danger-secondary-default"),
          500: nj("--ui-semantic-color-background-status-danger-solid-default"),
          600: nj("--ui-semantic-color-background-status-danger-solid-default"),
          700: nj("--ui-semantic-color-text-status-danger-contrast-default"),
          800: nj("--ui-semantic-color-text-status-danger-contrast-default"),
          900: nj("--ui-semantic-color-text-status-danger-contrast-default"),
        },
        green: {
          50: nj("--ui-semantic-color-background-status-success-tertiary-default"),
          100: nj("--ui-semantic-color-background-status-success-tertiary-default"),
          200: nj("--ui-semantic-color-background-status-success-secondary-default"),
          500: nj("--ui-semantic-color-background-status-success-solid-default"),
          600: nj("--ui-semantic-color-background-status-success-solid-default"),
          700: nj("--ui-semantic-color-text-status-success-contrast-default"),
          800: nj("--ui-semantic-color-text-status-success-contrast-default"),
          900: nj("--ui-semantic-color-text-status-success-contrast-default"),
        },
        amber: {
          50: nj("--ui-semantic-color-background-status-warning-tertiary-default"),
          100: nj("--ui-semantic-color-background-status-warning-tertiary-default"),
          200: nj("--ui-semantic-color-background-status-warning-secondary-default"),
          500: nj("--ui-semantic-color-background-status-warning-solid-default"),
          600: nj("--ui-semantic-color-background-status-warning-solid-default"),
          700: nj("--ui-semantic-color-text-status-warning-contrast-default"),
          800: nj("--ui-semantic-color-text-status-warning-contrast-default"),
          900: nj("--ui-semantic-color-text-status-warning-contrast-default"),
        },
        yellow: {
          400: nj("--ui-semantic-color-background-status-warning-solid-default"),
          500: nj("--ui-semantic-color-background-status-warning-solid-default"),
          800: nj("--ui-semantic-color-text-status-warning-contrast-default"),
        },
        blue: {
          50: nj("--ui-semantic-color-background-status-information-tertiary-default"),
          100: nj("--ui-semantic-color-background-status-information-tertiary-default"),
          200: nj("--ui-semantic-color-background-status-information-secondary-default"),
          500: nj("--ui-semantic-color-background-status-information-solid-default"),
          600: nj("--ui-semantic-color-background-status-information-solid-default"),
          700: nj("--ui-semantic-color-text-status-information-contrast-default"),
          800: nj("--ui-semantic-color-text-status-information-contrast-default"),
          900: nj("--ui-semantic-color-text-status-information-contrast-default"),
        },

        /* Surface : remplace `bg-white` sur les éléments de contenu.
         * `white` lui-même n'est PAS remappé — Tailwind n'a qu'une couleur pour
         * `bg-white` et `text-white`. CORRECTION du 2026-09-02 : la note d'origine
         * ajoutait « et le second habille le texte des boutons pleins, qui doit
         * rester blanc ». C'est FAUX en thème sombre — le blanc ne s'inverse pas
         * et tombe à 3,88:1 sur le bleu d'accent éclairci. Le texte sur fond plein
         * prend `text-inverse` (voir plus bas) ; `white` reste non remappé, mais
         * pour la seule raison qu'un remappage casserait `bg-white`. */
        surface: {
          DEFAULT: nj("--ui-semantic-color-background-neutral-primary-default"),
          muted: nj("--ui-semantic-color-background-neutral-secondary-default"),
        },
      },

      /* ——————————————————————————————————————————————————————————————————
       * CONTRASTE — le texte et les bordures ne suivent PAS la rampe de gris.
       *
       * Mappée sur les tokens `core`, l'échelle donnait `text-gray-400` = #425663
       * sur un fond #1a1f23 en sombre, soit ~2,2:1 : illisible. Les tokens `core`
       * sont documentés comme « privés, de référence » ; ce sont les SÉMANTIQUES
       * qui portent la garantie de contraste, et qui s'inversent proprement.
       * Tailwind accepte une échelle par propriété : on redirige donc `textColor`
       * et `borderColor` sans toucher une seule classe dans les écrans.
       * —————————————————————————————————————————————————————————————————— */
      textColor: {
        /* Encre POUR UN FOND PLEIN (bulle utilisateur, bouton bold, pastille).
         *
         * `text-white` en dur échoue en thème sombre : le bleu d'accent
         * s'éclaircit (#007BC5 → #3D86C4) alors que le blanc, lui, ne s'inverse
         * pas. Mesuré sur les tokens ET dans le navigateur, les deux méthodes
         * concordant : blanc sur bulle = 4,53:1 en clair mais 3,88:1 en sombre,
         * sous le seuil AA de 4,5 pour du texte de 16px. Cela touchait TOUS les
         * messages de l'utilisateur.
         *
         * `text-neutral-inverse` est l'encre que la charte prévoit pour ce cas,
         * et elle suit le thème : 4,53 en clair, 4,55 en sombre. La charte
         * interdit par ailleurs de remapper `white` lui-même — d'où ce rôle
         * distinct plutôt qu'un détournement. */
        inverse: nj("--ui-semantic-color-text-neutral-inverse-default"),

        gray: {
          50: nj("--ui-semantic-color-text-neutral-inverse-default"),
          100: nj("--ui-semantic-color-text-neutral-inverse-default"),
          200: nj("--ui-semantic-color-text-neutral-inverse-default"),
          /* Encres claires : `contrast` et NON `tertiary`.
           *
           * `tertiary` est calibré pour la surface PRIMAIRE seulement — mesuré
           * sur les tokens : 4,56:1 sur fond primaire (juste au-dessus d'AA),
           * mais 4,28:1 sur la surface SECONDAIRE, donc SOUS le seuil de 4,5.
           * Or `bg-gray-50` pointe justement la surface secondaire, et deux
           * pages entières (CostPage, BenchPage) l'ont en fond : tout leur texte
           * discret échouait AA, dans les DEUX thèmes.
           *
           * `contrast` est l'encre la plus claire de la charte qui passe sur les
           * deux surfaces (7,01 et 6,58) — la hiérarchie visuelle est préservée
           * (elle reste bien plus claire que `secondary` à 9,95 et `primary` à
           * 17,02), l'accessibilité aussi. Exigence explicite de TURBINE_LENS-70. */
          300: nj("--ui-semantic-color-text-neutral-contrast-default"),
          400: nj("--ui-semantic-color-text-neutral-contrast-default"),
          500: nj("--ui-semantic-color-text-neutral-contrast-default"),
          600: nj("--ui-semantic-color-text-neutral-secondary-default"),
          700: nj("--ui-semantic-color-text-neutral-secondary-default"),
          800: nj("--ui-semantic-color-text-neutral-primary-default"),
          900: nj("--ui-semantic-color-text-neutral-primary-default"),
        },
      },

      borderColor: {
        gray: {
          100: nj("--ui-semantic-color-border-neutral-minimal-default"),
          200: nj("--ui-semantic-color-border-neutral-subtle-default"),
          300: nj("--ui-semantic-color-border-neutral-moderate-default"),
          400: nj("--ui-semantic-color-border-neutral-strong-default"),
        },

        /* Bordures de STATUT — la passerelle des couleurs ne suffisait pas.
         *
         * `border-amber-200` et consorts retombaient sur l'échelle `colors`, qui
         * pointe des tokens de FOND. Une couleur de fond employée comme bordure
         * ne délimite rien : mesuré contre la surface de page, `border-amber-200`
         * donnait 1,50:1 — très en dessous des 3:1 qu'exige WCAG 1.4.11 pour la
         * limite d'un composant. Conséquence visible : le bandeau « lecture
         * seule » et le bandeau Genie se lisaient comme un lavis sans contour,
         * ce qui donne l'impression d'un défaut de contraste alors que leur
         * texte est à 6,0:1.
         *
         * On câble donc les vraies familles `border-status-*`. `moderate` est le
         * niveau le plus léger qui atteint le seuil (3,50 en ambre, 3,48 en bleu,
         * dans les deux thèmes) ; `strong` est réservé aux 300/400, pour les cas
         * où la limite doit porter seule. */
        amber: {
          200: nj("--ui-semantic-color-border-status-warning-moderate-default"),
          300: nj("--ui-semantic-color-border-status-warning-strong-default"),
        },
        red: {
          200: nj("--ui-semantic-color-border-status-danger-moderate-default"),
          300: nj("--ui-semantic-color-border-status-danger-strong-default"),
        },
        green: {
          200: nj("--ui-semantic-color-border-status-success-moderate-default"),
          300: nj("--ui-semantic-color-border-status-success-strong-default"),
        },
        blue: {
          200: nj("--ui-semantic-color-border-status-information-moderate-default"),
          300: nj("--ui-semantic-color-border-status-information-strong-default"),
        },
      },

      /* ————————————————————————————————————————————————————————————————
       * PASSERELLE 4 — les graisses.
       *
       * La charte ne définit que DEUX graisses sémantiques : `regular` (400) et
       * `bold` (700). Vérifié à deux sources indépendantes — les tokens
       * (`--ui-semantic-font-weight-regular|bold`, il n'y en a pas d'autres) et
       * le CSS compilé du design system, qui n'emploie que ces deux-là sur ses
       * 430 Ko de composants. Ni `medium` ni `semibold` n'existent.
       *
       * Le code hérité écrit `font-medium` (24 fois) et `font-semibold` (14).
       * Plutôt que de reprendre 38 endroits, on redirige les deux rôles :
       * - `medium` → **regular**, parce que Fluid met ses PROPRES boutons en
       *   regular (`--ui-btn-font-weight: var(--ui-semantic-font-weight-regular)`)
       *   et que c'est là que vit l'essentiel de nos `font-medium` ;
       * - `semibold` → **bold**, la seule graisse plus lourde disponible.
       * À supprimer quand plus aucun écran n'écrit medium/semibold.
       * ———————————————————————————————————————————————————————————————— */
      fontWeight: {
        medium: "var(--ui-semantic-font-weight-regular)",
        semibold: "var(--ui-semantic-font-weight-bold)",
      },

      fontFamily: {
        /* Polices produit de la charte. Les polices de marque (Clan Pro,
         * Right Grotesk) sont interdites en digital. */
        sans: ["Lato", "system-ui", "-apple-system", "sans-serif"],
        mono: ["Roboto Mono", "ui-monospace", "monospace"],
      },

      borderRadius: {
        /* Signature de Fluid 6 : les boutons n'ont pas d'arrondi. Vérifié dans le
         * CSS compilé du design system, la règle couvre aussi les CHAMPS
         * (`.ui-form-item__field-wrapper`), les CARTES (`.ui-card`) et les boutons
         * icône. Seuls `.ui-badge` (99rem) et `.ui-avatar` (50%) sont arrondis. */
        button: "0",
      },

      /* ————————————————————————————————————————————————————————————————
       * PASSERELLE 5 — élévation, opacité de désactivation, anneau de focus.
       *
       * Les couleurs et les graisses ne suffisaient pas : ces trois rôles ont
       * aussi un équivalent dans la charte, et les laisser sur les valeurs par
       * défaut de Tailwind produit des écarts réels, pas cosmétiques.
       *
       * OMBRES — les élévations de la charte sont SENSIBLES AU THÈME : alpha .1
       * en clair, .3 en sombre, et trois couches au lieu d'une. `shadow-sm` de
       * Tailwind est une ombre claire fixe, donc quasi invisible sur une surface
       * sombre. On mappe l'échelle Tailwind sur les six paliers `-dp`.
       *
       * DÉSACTIVATION — la charte dit 0,35 (`--ui-semantic-opacity-disabled`),
       * le code écrit `opacity-40`. On redirige le rôle plutôt que les 4 usages.
       *
       * FOCUS — la charte spécifie 2px de large ET 2px de décalage
       * (`--ui-semantic-size-border-focus-ring-width|offset`). La couleur du
       * décalage doit venir de la surface, sinon Tailwind dessine un liseré
       * BLANC autour de l'anneau en thème sombre.
       * ———————————————————————————————————————————————————————————————— */
      boxShadow: {
        sm: "var(--ui-semantic-elevation-shadow-2-dp)",
        DEFAULT: "var(--ui-semantic-elevation-shadow-4-dp)",
        md: "var(--ui-semantic-elevation-shadow-6-dp)",
        lg: "var(--ui-semantic-elevation-shadow-8-dp)",
        xl: "var(--ui-semantic-elevation-shadow-16-dp)",
        "2xl": "var(--ui-semantic-elevation-shadow-24-dp)",
      },

      opacity: {
        40: "var(--ui-semantic-opacity-disabled)",
      },

      ringWidth: {
        2: "var(--ui-semantic-size-border-focus-ring-width)",
      },
      ringOffsetWidth: {
        2: "var(--ui-semantic-size-border-focus-ring-offset)",
      },
      ringOffsetColor: {
        /* `var()` NU, surtout pas le helper `nj()` : Tailwind ne substitue pas
         * `<alpha-value>` sur cette clé, et la valeur sortait telle quelle dans
         * le CSS — `rgb(from … / <alpha-value>)` est invalide, donc l'anneau se
         * dessinait avec une couleur cassée sans qu'aucune erreur ne le signale.
         * Mesuré : 1 occurrence de `<alpha-value>` littéral dans le CSS produit.
         * Ici on n'a de toute façon pas besoin des modificateurs d'opacité. */
        DEFAULT: "var(--ui-semantic-color-background-neutral-primary-default)",
      },
    },
  },
  plugins: [],
};

export default config;
