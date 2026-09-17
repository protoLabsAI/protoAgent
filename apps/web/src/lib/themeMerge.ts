// Pure theme-blob precedence logic (#1762) — no DOM, no DS import, so it unit-tests
// standalone and can't drag `@protolabsai/ui/theming` (or `window`) into the merge.
//
// The console round-trips an opaque `{mode, overrides}` blob (the DS ThemePanel's
// schema, persisted under localStorage "pl-theme"): `overrides` maps `--pl-*` CSS
// vars → values, `mode` is the light/dark toggle. These helpers decide, on boot vs.
// on an agent switch, which blob wins — so a user's persisted tweaks aren't clobbered
// by the agent/server default on every reload.

export type ThemeMode = "light" | "dark";

/** The opaque per-agent theme blob. `mode`/`overrides` are the DS contract; unknown
 *  keys are preserved (forward-compat with future DS token groups) with user-wins. */
export type ThemeBlob = {
  mode?: ThemeMode;
  overrides?: Record<string, string>;
  [key: string]: unknown;
};

/** Coerce an agent/user-supplied value into a sanitized {mode, overrides, …} blob,
 *  or `null` when it carries nothing applyable. Defensive: `mode` is validated to
 *  light/dark, and `overrides` is filtered to string-valued `--pl-*` tokens so a
 *  malformed or hostile blob can't inject arbitrary CSS vars. Unknown top-level keys
 *  are kept verbatim so a newer DS blob shape survives the round-trip. */
export function normalizeThemeBlob(value: unknown): ThemeBlob | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;

  const overrides: Record<string, string> = {};
  if (v.overrides && typeof v.overrides === "object" && !Array.isArray(v.overrides)) {
    for (const [k, val] of Object.entries(v.overrides as Record<string, unknown>)) {
      if (k.startsWith("--pl-") && typeof val === "string") overrides[k] = val;
    }
  }

  const mode: ThemeMode | undefined = v.mode === "light" ? "light" : v.mode === "dark" ? "dark" : undefined;
  const extraKeys = Object.keys(v).filter((k) => k !== "mode" && k !== "overrides");

  // Nothing to apply (no mode, no overrides, no forward-compat keys) → treat as absent
  // so callers fall back to the design-system defaults instead of stamping data-theme.
  if (mode === undefined && Object.keys(overrides).length === 0 && extraKeys.length === 0) return null;

  const out: ThemeBlob = {};
  for (const k of extraKeys) out[k] = v[k];
  if (mode !== undefined) out.mode = mode;
  out.overrides = overrides;
  return out;
}

/** Merge a DEFAULT theme blob with a USER-OVERRIDE blob so user overrides WIN and the
 *  default only fills the gaps (#1762). Per-token precedence: any `--pl-*` the user set
 *  beats the default; a token the user didn't set falls back to the default. `mode` is
 *  scalar — the user's choice wins when present, else the default's (else "dark"). Unknown
 *  top-level keys also take the user's value when present. Returns `null` only when BOTH
 *  inputs are empty/absent (→ design-system defaults).
 *
 *  Theme families (the DS panel's `preset`, `edits`, `saved`). A family variant is a
 *  complete look for its mode, so gap-filling only makes sense WITHIN one family:
 *  - The working copy's family differs from the default's — another family, a family over a
 *    hand-tuned default, or no family (a saved preset / import / reset look) over a family
 *    default → the working copy wins wholesale. Filling its gaps would paint the other look's
 *    tokens into it (e.g. a light variant inheriting a dark-tuned accent-fg at 1.5:1).
 *  - Same family → the per-token merge. `edits` = the user's, plus the default's edits to
 *    tokens the user didn't set (those merged values really are the default's edits); a token
 *    the user DID set but didn't list is the family's own value, not an edit. `saved` is never
 *    inherited: a saved look the user hasn't applied would mark the wrong gallery tile.
 *  - Neither has a family → the original per-token merge (#1762). */
export function mergeTheme(defaults: unknown, overrides: unknown): ThemeBlob | null {
  const base = normalizeThemeBlob(defaults);
  const user = normalizeThemeBlob(overrides);
  if (!base && !user) return null;

  const b: ThemeBlob = base ?? {};
  const u: ThemeBlob = user ?? {};
  if (user && u.preset !== b.preset) {
    const whole: ThemeBlob = { ...u, overrides: { ...(u.overrides ?? {}) } };
    if (whole.mode === undefined && b.mode !== undefined) whole.mode = b.mode;
    return whole;
  }
  const merged: ThemeBlob = {
    ...b,
    ...u,
    overrides: { ...(b.overrides ?? {}), ...(u.overrides ?? {}) },
  };
  const mode = u.mode ?? b.mode;
  if (mode !== undefined) merged.mode = mode;
  else delete merged.mode;
  if (user && typeof merged.preset === "string") {
    const userSet = u.overrides ?? {};
    const own = Array.isArray(u.edits) ? (u.edits as string[]) : [];
    const inherited = Array.isArray(b.edits) ? (b.edits as string[]).filter((k) => !(k in userSet)) : [];
    merged.edits = [...new Set([...own, ...inherited])];
    if (u.saved === undefined) delete merged.saved;
  }
  return merged;
}

/** Decide which blob to persist + apply for a given theme change.
 *
 *  - On BOOT (`preservePersisted`), the user's persisted working copy WINS over the
 *    incoming agent/server default — so an unsaved tweak survives a reload and defaults
 *    only fill the gaps. This is the fix for "defaults clobber user overrides" (#1762):
 *    the apply reads persisted state, it doesn't reset to the default blob.
 *  - On an explicit SWITCH/RESET, we adopt the incoming agent theme verbatim (ADR 0042 —
 *    switching agents repaints to that agent's saved look), ignoring the persisted copy. */
export function resolveThemeToPersist(
  incoming: unknown,
  persisted: unknown,
  opts: { preservePersisted?: boolean } = {},
): ThemeBlob | null {
  return opts.preservePersisted ? mergeTheme(incoming, persisted) : normalizeThemeBlob(incoming);
}
