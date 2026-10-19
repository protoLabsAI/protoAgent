/**
 * VENDORED — @protolabsai/design@0.13.0, the token tree (src/tokens.js), byte-for-byte.
 *
 * The relative dependency of the vendored `./embed` module (pl-design-embed.mjs), which does
 * `import { tokens, flatten } from "./tokens.js"`. The browser resolves that specifier against
 * the flat /plugins/artifact/vendor route, so this file MUST keep the name `tokens.js` and sit
 * beside the embed module. It has no imports of its own, so the pair is closed.
 *
 * DO NOT EDIT BY HAND — copy verbatim from the package. tests/test_artifact_plugin.py asserts
 * this file is byte-identical to the installed @protolabsai/design (header aside) when present.
 * Refs protoLabsAI/protoContent#577, protoLabsAI/protoContent#578.
 */
/**
 * @protolabsai/design — canonical brand tokens. THE single source of truth.
 *
 * Edit here, then `pnpm build` regenerates dist/tokens.json + dist/tokens.css.
 * Nothing downstream (CSS vars, Tailwind preset, docs) is hand-maintained —
 * it all derives from these objects, so the spec and the shipped values can't
 * drift apart again (which is exactly what bit us before this package existed).
 *
 * Locked 2026-05-29 (dark). Light mode added 2026-06-09.
 *
 * THEMING / WHITE-LABEL
 * --------------------
 * `tokens` holds the DEFAULT (protoLabs.studio dark) theme. `light` holds the light-mode
 * COLOR overrides (same keys). The build emits:
 *   :root { ...dark... }                          — default
 *   @media (prefers-color-scheme: light){:root}   — OS preference drives light
 *   :root[data-theme="light"|"dark"]              — explicit force (beats the media query)
 * Marketing stays dark by setting [data-theme="dark"]; apps inherit OS preference.
 * White-label: a consumer overrides the `--pl-color-*` (esp. accent/bg/fg) under
 * their own scope — our values are the default, theirs win where set.
 */

export const tokens = {
  color: {
    // ── Brand marks (theme-invariant — the literal brand colors) ──
    brand: {
      // Lavender leads on dark; indigo leads on light. Each anchor has a
      // light / base / deep step (the deep steps clear AA on white).
      lavender: "#9b87f2", // base — brand accent + dark-mode interactive
      lavenderLight: "#b8a6f5", // hover / link-lift on dark
      lavenderDeep: "#5b3fc9", // deep step — light grounds / white-label
      indigo: "#6366f1", // secondary anchor + light-mode interactive accent
      indigoBright: "#818cf8", // gradient midpoint
      indigoDeep: "#4f46e5", // deep step — light-mode links / gradient end
    },
    // ── Surfaces ──
    bg: "#0a0a0c", // default ground
    bgRaised: "#131316", // cards, panels, slight elevation
    bgSubtle: "#1c1c22", // table stripes, list hover targets
    bgHover: "#19191d", // interactive hover wash
    bgInset: "#060608", // sunken — code wells, deep inputs
    overlay: "rgba(0, 0, 0, 0.6)", // modal / drawer scrim
    // ── Text (three tiers + on-accent) ──
    fg: "#ededed", // primary
    fgMuted: "#a1a1aa", // secondary / labels
    fgSubtle: "#71717a", // tertiary / placeholder / disabled
    fgOnAccent: "#0a0a0c", // text/icon on an accent fill
    // ── Lines ──
    border: "rgba(255, 255, 255, 0.08)", // subtle 1px dividers
    borderStrong: "rgba(255, 255, 255, 0.18)", // emphasized borders, buttons
    // ── Interactive accent (the white-label swap point; = brand lavender) ──
    accent: "#9b87f2",
    accentHover: "#b8a6f5",
    accentFg: "#b8a6f5", // accent as text (links, active labels) on this ground
    focus: "#9b87f2", // focus-visible ring
    // ── Status — low-chroma, never neon. They mean something; not decoration. ──
    status: {
      success: "oklch(0.72 0.13 145)",
      warning: "oklch(0.78 0.12 75)",
      error: "oklch(0.64 0.16 25)",
      info: "oklch(0.68 0.12 230)",
    },
    // ── Charts — 8 categorical series + semantic helpers ──
    // Luminance >= 0.65 so every swatch reads against the dark ground (#0a0a0c).
    // series1 anchors on brand lavender; the rest span distinguishable hues.
    // Low-chroma OKLCH throughout — never neon.
    chart: {
      series1: "oklch(0.72 0.14 290)", // brand lavender anchor
      series2: "oklch(0.70 0.12 250)", // sky
      series3: "oklch(0.72 0.13 170)", // teal
      series4: "oklch(0.74 0.14 135)", // emerald
      series5: "oklch(0.78 0.12 80)",  // amber
      series6: "oklch(0.70 0.14 30)",  // warm rose
      series7: "oklch(0.68 0.12 210)", // steel
      series8: "oklch(0.70 0.13 320)", // violet
      grid: "rgba(255, 255, 255, 0.06)", // subtle axis lines, ~border opacity
      axis: "#a1a1aa", // tick text — matches fgMuted
    },
  },
  gradient: {
    // The one gradient — hero text fills + large brand moments only. Not UI chrome.
    // Built from `var(--pl-*)` (not literal hex) so it responds to theme/white-label
    // overrides: the lead stop is `--pl-color-accent` (the white-label swap point, = brand
    // lavender by default → default look unchanged), the mid/end stay brand-anchored so it
    // stays on-brand. This is what makes the Splash wordmark actually follow the theme.
    brand: "linear-gradient(135deg, var(--pl-color-accent) 0%, var(--pl-color-brand-indigo-bright) 50%, var(--pl-color-brand-indigo) 100%)",
    brandLight: "linear-gradient(135deg, var(--pl-color-accent) 0%, var(--pl-color-brand-indigo) 50%, var(--pl-color-brand-indigo-deep) 100%)",
  },
  shadow: {
    glow: "0 0 80px rgba(155, 135, 242, 0.08)", // subtle lavender halo; brand moments only
    // A tiny 2-step UI elevation scale:
    //   card    — an in-flow raised card, resting IN the document (report cards, notes).
    //   popover — a floating overlay OVER the document (popovers, palette, dialogs, drawers, toasts).
    // Reach for `card` on a static raised block; the lofted `popover` reads as a halo on one.
    card: "0 1px 3px rgba(0, 0, 0, 0.35)",
    popover: "0 8px 28px rgba(0, 0, 0, 0.5)",
  },
  font: {
    sans: '"Geist", system-ui, -apple-system, sans-serif',
    mono: '"Geist Mono", ui-monospace, "SF Mono", monospace',
    baseSize: "14px", // tools-grade density — keep equal to size.base below
    // Theme-invariant type scale. Size doesn't vary by theme, so it lives here only
    // (never in `light`). Emitted as --pl-font-size-<step> (3xs/2xs stay digit-leading).
    // size.base MUST stay equal to baseSize above — base is the anchor, not a new value.
    size: { "3xs": "10px", "2xs": "11px", xs: "12px", sm: "13px", base: "14px", lg: "16px", xl: "18px", display: "28px" },
    lineHeightBody: "1.6",
    lineHeightHeading: "1.2",
    weight: { body: "440", medium: "500", semibold: "600" }, // Geist 400 reads thin on dark — a subtle lift, not bold
  },
  // Radius scale — reads engineered; never 0, never bubbly. `radius` is the base
  // (do NOT nest it into an object — that would rename --pl-radius). The md/lg/xl/pill
  // steps flatten to --pl-radius-md/-lg/-xl/-pill. Theme-invariant: nothing in `light`.
  radius: "4px", // base — controls, inputs, chips, most surfaces
  radiusMd: "6px", // cards and panels needing slightly softer corners
  radiusLg: "8px", // dialogs, drawers, popovers
  radiusXl: "12px", // LARGE OVERLAYS ONLY — full-screen sheets, launcher panel
  radiusPill: "999px", // badges, avatars, pills — never on rectangles
  // Spacing scale. The integer steps are the backbone (1=4px … 12=48px); the
  // three half-steps fill the gaps that kept forcing off-scale one-offs. Keys
  // carry `_` not `.` because a custom-property name can't contain a dot —
  // flatten() emits --pl-space-0_5 / -1_5 / -2_5. JS lists integer-like keys
  // first, so these three sort AFTER 12 in Object.keys order.
  //   0_5 (2px)  — hairline/nudge: icon-to-text offsets, tight chip padding.
  //   1_5 (6px)  — dense gaps between closely-set controls.
  //   2_5 (10px) — the rung between 8 and 12.
  // Deliberately NOT added: 3/5/7/9/14px (snap 3→2|4, 5→4|6, 7→6|8, 9→8|10, 14→12|16).
  space: {
    1: "4px",
    2: "8px",
    3: "12px",
    4: "16px",
    5: "20px",
    6: "24px",
    8: "32px",
    12: "48px",
    "0_5": "2px",
    "1_5": "6px",
    "2_5": "10px",
  },
  borderWidth: "1px",
  motion: {
    fast: "120ms", // hover (shipped)
    base: "200ms", // page transitions
    theme: "400ms", // theme switch
    loading: "1000ms",
    status: "2000ms", // status pulses
    ease: "ease",
    easeInOut: "ease-in-out",
    linear: "linear",
  },
};

/**
 * Light-mode COLOR overrides (apps; marketing stays dark). Same keys as
 * tokens.color, minus `brand` (the marks are invariant). Status colors are
 * darkened for contrast on a light ground; the accent darkens to clear AA on
 * white. Surfaces invert their elevation relation (cards become the lightest).
 */
export const light = {
  color: {
    // Cool-neutral surfaces (matching the cool-purple brand), cards the lightest.
    bg: "#f6f7f9", // cool off-white ground
    bgRaised: "#ffffff", // cards / panels — the lightest surface (elevation flips)
    bgSubtle: "#eceef2", // table stripes, list hover targets
    bgHover: "#e4e7ec", // interactive hover wash
    bgInset: "#eef0f4", // sunken — code wells, deep inputs
    overlay: "rgba(20, 20, 30, 0.35)", // slightly cool scrim
    fg: "#18181b", // primary
    fgMuted: "#52525b", // secondary / labels
    fgSubtle: "#8a8a94", // tertiary / placeholder
    fgOnAccent: "#ffffff", // text on the indigo accent fill
    border: "rgba(0, 0, 0, 0.10)",
    borderStrong: "rgba(0, 0, 0, 0.20)",
    // Indigo leads light: an indigo fill, indigo-deep for links/accent text (AA). The fill
    // sits a hair under brand.indigo (#6366f1, L−.006) — that exact mark carries white
    // text at 4.47:1, just under AA; #6164ef clears it at 4.59 and reads identically.
    accent: "#6164ef",
    accentHover: "#4f46e5",
    accentFg: "#4f46e5",
    focus: "#6164ef",
    // Status — used as accents (dots, borders, badge tints), so they don't chase
    // body-text AA, but they must clear 3:1 on bg AND raised (WCAG 1.4.11 non-text
    // contrast; ThemePanel's rulebook enforces it). Warning is the lightest, most
    // yellow gold that does: a brighter yellow can't reach 3:1 on a near-white
    // ground (the old oklch(.74 .16 92) was 2.15:1). Info sits at h240 — 37° clear of
    // the indigo accent (h277; the old h255 was 22° away at the same lightness) and in
    // line with the dark-mode info hue.
    status: {
      success: "oklch(0.62 0.15 150)",
      warning: "oklch(0.645 0.132 94)",
      error: "oklch(0.58 0.20 25)",
      info: "oklch(0.58 0.133 240)",
    },
    // Light-ground chart — lower luminance so swatches read on white.
    chart: {
      series1: "oklch(0.52 0.16 290)", // brand lavender anchor
      series2: "oklch(0.50 0.14 250)", // sky
      series3: "oklch(0.52 0.15 170)", // teal
      series4: "oklch(0.54 0.16 135)", // emerald
      series5: "oklch(0.58 0.14 80)",  // amber
      series6: "oklch(0.50 0.16 30)",  // warm rose
      series7: "oklch(0.48 0.14 210)", // steel
      series8: "oklch(0.50 0.15 320)", // violet
      grid: "rgba(0, 0, 0, 0.06)", // subtle axis lines
      axis: "#52525b", // tick text — matches light fgMuted
    },
  },
  gradient: { brand: tokens.gradient.brandLight },
  shadow: { card: "0 1px 3px rgba(0, 0, 0, 0.08)", popover: "0 8px 24px rgba(0, 0, 0, 0.12)" },
};

/* ────────────────────────────────────────────────────────────────────────────
 * Resolved values — for renderers with no CSS-variable layer.
 *
 * Most consumers read `var(--pl-*)` and inherit theming/white-label for free.
 * Server-side image renderers can't: Satori / @vercel/og rasterize inline
 * styles with no cascade and no custom properties, so an OG card has to bake in
 * literal values. That boundary is exactly where the single-source guarantee
 * above used to fail — the OG renderers hand-copied hex and drifted to a
 * pre-lavender `#a78bfa` that the live brand had already moved off of.
 *
 * The fix is not a second copy of the palette; it's deriving the literals from
 * the same token objects, so a brand change still propagates in one edit.
 * ──────────────────────────────────────────────────────────────────────────── */

/** kebab-case one token path segment (`lavenderLight` → `lavender-light`). */
export const kebab = (s) => String(s).replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();

/** Flatten a nested token object into the `--pl-*` variable map the build emits. */
export function flatten(obj, prefix = "--pl", acc = {}) {
  for (const [k, v] of Object.entries(obj)) {
    const name = `${prefix}-${kebab(k)}`;
    if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, name, acc);
    else acc[name] = String(v);
  }
  return acc;
}

/**
 * Replace every `var(--pl-…)` reference in a token value with its literal value.
 *
 * Resolves repeatedly so a token defined in terms of another token still lands
 * on a literal, and throws on an unknown var rather than silently emitting an
 * unresolved `var()` that a rasterizer would drop on the floor.
 *
 * @param {string} value  a token value, e.g. tokens.gradient.brand
 * @param {{theme?: "dark"|"light"}} [opts]  which palette to resolve against
 */
export function resolveTokenVars(value, { theme = "dark" } = {}) {
  const map = theme === "light" ? { ...flatten(tokens), ...flatten(light) } : flatten(tokens);
  let out = String(value);
  // Depth-bounded: each pass resolves one level of indirection. The token tree
  // is shallow, so anything still unresolved after this is a cycle.
  for (let pass = 0; pass < 10 && out.includes("var("); pass++) {
    // `_` is in the class so half-step spacing vars (--pl-space-0_5/-1_5/-2_5)
    // resolve — a dot can't be a custom-property char, so the keys carry `_`.
    out = out.replace(/var\(\s*(--pl-[a-z0-9_-]+)\s*(?:,\s*([^()]*))?\)/g, (whole, name, fallback) => {
      if (name in map) return map[name];
      if (fallback !== undefined) return fallback.trim();
      throw new Error(`resolveTokenVars: unknown token ${name} in "${value}"`);
    });
  }
  if (out.includes("var(")) throw new Error(`resolveTokenVars: cyclic reference in "${value}"`);
  return out;
}

/**
 * The brand gradient with literal stops — the OG/Satori-safe form of
 * `tokens.gradient.brand`. Derived, never hand-written.
 *
 * Note each mode resolves its OWN gradient string: `light` re-orders the stops,
 * it isn't the dark gradient with light colors swapped in. Passing the dark
 * string with `{theme:"light"}` yields a degenerate ramp (indigo→indigo), so
 * reach for `brandGradientCssLight` rather than resolving by hand.
 */
export const brandGradientCss = resolveTokenVars(tokens.gradient.brand);

/** Light-mode counterpart of {@link brandGradientCss}. */
export const brandGradientCssLight = resolveTokenVars(light.gradient.brand, { theme: "light" });

/**
 * The brand gradient's colors, lead → end, for renderers that need to compose
 * their own stop list (a two-stop card variant, an SVG `<linearGradient>`).
 */
export const brandGradientStops = brandGradientCss
  .match(/#[0-9a-f]{3,8}|(?:rgb|hsl|oklch)a?\([^)]*\)/gi)
  ?? [];

export default tokens;
