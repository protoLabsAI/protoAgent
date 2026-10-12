/**
 * VENDORED — @protolabsai/design@0.12.0, the `./embed` entry (src/embed.js), byte-for-byte.
 *
 * The artifact shell serves this file as-is from /plugins/artifact/vendor and loads it as a
 * same-origin module (there is no bundler / node_modules at runtime). Its `./tokens.js` import
 * is UNEDITED: tokens.js is vendored beside it (the same flat /vendor dir), so the browser
 * resolves the relative specifier against this module — both names are in _VENDOR_FILES
 * (_routes.py). embed.js is NOT self-contained: vendoring it alone would 404 that import at
 * load time, so the pair is vendored together and tests/test_artifact_plugin.py guards the closure.
 *
 * DO NOT EDIT BY HAND — copy verbatim from the package. tests/test_artifact_plugin.py asserts
 * this module is byte-identical to the installed @protolabsai/design (header aside) when present.
 * Refs protoLabsAI/protoContent#577, protoLabsAI/protoContent#578.
 */

/**
 * @protolabsai/design/embed — hand the active theme to a sandboxed frame as literal values.
 *
 * A cross-origin / CSP-locked frame (an artifact iframe, a plugin frame component) can't read
 * the host stylesheet, so the host reads its own computed `--pl-*` tokens and ships them in as
 * a flat snapshot the frame re-declares on its own `:root`. protoAgent hand-rolled this inside
 * the artifact shell (`THEME_TOKENS` / `themeTokens()`) and the plugin-frame handshake
 * (`consoleTheme()`); this is the DS-owned, derived form of the same handoff.
 *
 * PURE + SSR-safe: the module touches NO DOM at import time. `EMBED_TOKENS` is derived from the
 * token tree (never a hand list), and `themeTokenSnapshot` resolves `getComputedStyle` from the
 * ELEMENT it's handed (`el.ownerDocument.defaultView`), so it imports cleanly under Node / SSR
 * and is unit-testable with a stub element. Plain ES2017 (no optional chaining / nullish).
 */

import { tokens, flatten } from "./tokens.js";

// The flattened token GROUPS that cross into a frame: the full semantic colour palette
// (chart.* included — it lives under color.chart), the type system (families, the size scale,
// weights and line heights), the radius scale and the spacing scale. Gradient / shadow / motion
// / border-width are console chrome, not frame theming, so they're deliberately left out. The
// NAMES are never spelled out here — they're whatever flatten() emits for these groups, so a
// token added to tokens.js shows up in EMBED_TOKENS automatically.
const EMBED_GROUPS = /^--pl-(?:color|font|radius|space)(?:-|$)/;

/**
 * Every `--pl-*` token a sandboxed frame needs to mirror the host theme. GENERATED from the
 * token tree via flatten(), frozen and sorted. A superset of the protoAgent artifact shell's
 * THEME_TOKENS (embed.test.mjs pins that by listing THEME_TOKENS literally).
 */
export const EMBED_TOKENS = Object.freeze(
  Object.keys(flatten(tokens))
    .filter((name) => EMBED_GROUPS.test(name))
    .sort(),
);

// A well-formed `--pl-*` custom-property name, the name guard snapshotToCss applies to each key.
// `_` is a valid, inert character in a custom-property name, so the three spacing half-steps
// (--pl-space-0_5 / -1_5 / -2_5) are accepted and round-trip intact — EMBED_TOKENS publishes
// them, so the serializer must emit them. The frame-injection guard is the VALUE check
// (CSS_VALUE_HOSTILE, `<{};`) below, not the name charset: a hostile key like `--pl-x;}` still
// fails this pattern and is dropped.
const PL_VAR_NAME = /^--pl-[a-z0-9_-]+$/;

// Characters that would let a value break out of its declaration or the rule and inject more
// CSS. A value carrying any of them is dropped wholesale — never escaped.
const CSS_VALUE_HOSTILE = /[<{};]/;

/**
 * Read the host's computed `--pl-*` values off `el` into a flat `{ name: value }` snapshot.
 *
 * - `getComputedStyle` is resolved from `el.ownerDocument.defaultView`, so nothing touches a
 *   global `window`/`document` and the module stays import-time pure (SSR-safe);
 * - each name is read via `getPropertyValue(name).trim()`; empty values are omitted;
 * - a `"color-scheme"` key is added from `el`'s computed `color-scheme` when non-empty, else the
 *   `data-theme` ("dark"/"light") of the nearest `[data-theme]` ancestor, else omitted.
 *
 * @param {Element} el                    the host element to read the live theme from
 * @param {readonly string[]} [names]     which token names to read (defaults to EMBED_TOKENS)
 * @returns {Record<string,string>}
 */
export function themeTokenSnapshot(el, names) {
  if (names === undefined) names = EMBED_TOKENS;
  const out = {};
  if (!el) return out;
  const view = el.ownerDocument && el.ownerDocument.defaultView;
  const getComputed = view && view.getComputedStyle;
  if (!getComputed) return out;
  const cs = getComputed.call(view, el);

  for (const name of names) {
    const raw = cs.getPropertyValue(name);
    const value = (raw == null ? "" : String(raw)).trim();
    if (value) out[name] = value;
  }

  // color-scheme: the element's own computed value wins (so native controls — inputs,
  // scrollbars, date pickers — match the host); otherwise fall back to the nearest data-theme
  // force; otherwise omit it entirely.
  const rawScheme = cs.getPropertyValue("color-scheme");
  let scheme = (rawScheme == null ? "" : String(rawScheme)).trim();
  if (!scheme && el.closest) {
    const themed = el.closest("[data-theme]");
    const forced = themed ? themed.getAttribute("data-theme") : "";
    if (forced) scheme = String(forced).trim();
  }
  if (scheme) out["color-scheme"] = scheme;

  return out;
}

/**
 * Serialise a snapshot to a single CSS rule `selector{--pl-x:v;…;color-scheme:…}` the frame can
 * inject. The frame-injection guard runs per declaration:
 *   - a value containing `<`, `{`, `}` or `;` is DROPPED (never escaped);
 *   - every emitted name must match `/^--pl-[a-z0-9_-]+$/` (underscore included, so the spacing
 *     half-steps survive) — the sole non-`--pl-` exception is the well-known `color-scheme`
 *     property, forwarded for native-control theming.
 * Snapshot key order is preserved, so a snapshot from themeTokenSnapshot lands with its tokens
 * first and `color-scheme` last.
 *
 * @param {Record<string,string>} snapshot
 * @param {string} [selector]  defaults to ":root"
 * @returns {string}  the CSS rule, or "" if nothing survived the guard
 */
export function snapshotToCss(snapshot, selector) {
  if (selector === undefined) selector = ":root";
  if (!snapshot || typeof snapshot !== "object") return "";
  const decls = [];
  for (const name of Object.keys(snapshot)) {
    const rawValue = snapshot[name];
    if (typeof rawValue !== "string") continue;
    const value = rawValue.trim();
    if (!value) continue;
    if (CSS_VALUE_HOSTILE.test(value)) continue;
    if (name === "color-scheme") { decls.push("color-scheme:" + value); continue; }
    if (!PL_VAR_NAME.test(name)) continue;
    decls.push(name + ":" + value);
  }
  if (!decls.length) return "";
  return selector + "{" + decls.join(";") + "}";
}
