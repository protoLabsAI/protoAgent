/**
 * VENDORED — @protolabsai/design@0.12.0, the `./vega` entry (src/vega.js).
 *
 * The artifact shell serves this file as-is from /plugins/artifact/vendor and loads it as a
 * same-origin module (there is no bundler / node_modules at runtime). It then injects
 * `buildVegaConfig.toString()` / `vegaTooltipTheme.toString()` into the nonce-CSP chart frame
 * beside `artVega` — both functions are SELF-CONTAINED (no imports, no closures, plain ES2017),
 * so they reproduce verbatim in that bare context. Only these two exports are vendored; the
 * package's `vegaTheme` / `VEGA_THEME_TOKENS` (which import ./tokens.js) are not needed here.
 *
 * DO NOT EDIT BY HAND — copy verbatim from the package. tests/test_artifact_vega.py asserts
 * these functions are byte-identical to the installed @protolabsai/design when present.
 * Refs protoLabsAI/protoContent#571, protoLabsAI/protoContent#578.
 */

/**
 * The core: a flat `--pl-*` token map in, a Vega-Lite `config` object out.
 *
 * Self-contained by contract — every helper is declared inside, so
 * `buildVegaConfig.toString()` evaluated in a bare context reproduces this output
 * (see vega.test.mjs r1). Keep it that way: no outer references, no optional chaining.
 *
 * Categorical colours (`range.category`, `mark`, `rule`, …) stay as the raw token
 * strings — an SVG paint renders `oklch()` fine. The sequential/diverging ramps are
 * the exception: Vega interpolates those stops with d3-color, which does NOT parse
 * `oklch()`, so every ramp/heatmap/diverging stop (and every `mix()` input) is first
 * converted to an sRGB `#rrggbb` hex below.
 *
 * @param {Record<string,string>} tok  flat `{"--pl-color-bg": "...", ...}` map
 * @returns {Record<string, unknown>}  a Vega-Lite `config` object
 */
export function buildVegaConfig(tok) {
  tok = tok || {};
  function t(k, d) {
    var v = tok[k];
    return (typeof v === "string" && v.trim()) ? v.trim() : d;
  }

  // ── colour → sRGB [r,g,b] (0–255, gamma-encoded). null if unparseable. ──
  function clamp01(x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
  // Linear-light → sRGB transfer (gamma encode).
  function enc(x) {
    x = clamp01(x);
    return x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055;
  }
  function srgb(str) {
    if (typeof str !== "string") return null;
    var s = str.trim(), m;
    if ((m = /^#([0-9a-f]{3})$/i.exec(s))) {
      var h = m[1];
      return [parseInt(h[0] + h[0], 16), parseInt(h[1] + h[1], 16), parseInt(h[2] + h[2], 16)];
    }
    if ((m = /^#([0-9a-f]{6})$/i.exec(s))) {
      var x = m[1];
      return [parseInt(x.slice(0, 2), 16), parseInt(x.slice(2, 4), 16), parseInt(x.slice(4, 6), 16)];
    }
    if ((m = /^rgba?\(\s*([0-9.]+)[,\s]+([0-9.]+)[,\s]+([0-9.]+)/i.exec(s))) {
      return [Math.round(parseFloat(m[1])), Math.round(parseFloat(m[2])), Math.round(parseFloat(m[3]))];
    }
    // oklch(L C H): L a percentage or 0–1. Path: oklch → oklab → linear sRGB → gamma → clamp.
    if ((m = /^oklch\(\s*([0-9.]+)(%?)\s+([0-9.]+)\s+([0-9.]+)/i.exec(s))) {
      var L = parseFloat(m[1]); if (m[2] === "%") L = L / 100;
      var C = parseFloat(m[3]);
      var H = parseFloat(m[4]) * Math.PI / 180;
      var a = C * Math.cos(H), b = C * Math.sin(H);
      var l_ = L + 0.3963377774 * a + 0.2158037573 * b;
      var m_ = L - 0.1055613458 * a - 0.0638541728 * b;
      var s_ = L - 0.0894841775 * a - 1.2914855480 * b;
      var ll = l_ * l_ * l_, mm = m_ * m_ * m_, ss = s_ * s_ * s_;
      var r = 4.0767416621 * ll - 3.3077115913 * mm + 0.2309699292 * ss;
      var g = -1.2684380046 * ll + 2.6097574011 * mm - 0.3413193965 * ss;
      var bl = -0.0041960863 * ll - 0.7034186147 * mm + 1.7076147010 * ss;
      return [Math.round(enc(r) * 255), Math.round(enc(g) * 255), Math.round(enc(bl) * 255)];
    }
    return null;
  }
  function hx2(n) {
    n = Math.round(n); if (n < 0) n = 0; if (n > 255) n = 255;
    var s = n.toString(16);
    return s.length === 1 ? "0" + s : s;
  }
  function toHex(rgb) { return "#" + hx2(rgb[0]) + hx2(rgb[1]) + hx2(rgb[2]); }
  // A colour as `#rrggbb`, always — falls back through the accent to a literal so a ramp
  // stop can never leak a non-hex value into d3-color.
  function hexOf(str) { return toHex(srgb(str) || srgb(accent) || [155, 135, 242]); }
  // Linear blend of two colours in sRGB, `tt` of the way from `aStr` to `bStr`, as hex.
  function mix(aStr, bStr, tt) {
    var a = srgb(aStr) || [0, 0, 0], b = srgb(bStr) || [0, 0, 0];
    return toHex([a[0] + (b[0] - a[0]) * tt, a[1] + (b[1] - a[1]) * tt, a[2] + (b[2] - a[2]) * tt]);
  }

  // ── resolved values (fallbacks per shell.js: series → accent, grid → border,
  //    axis → fg-muted, then literal defaults) ──
  var bg = t("--pl-color-bg", "#0a0a0c");
  var fg = t("--pl-color-fg", "#ededed");
  var muted = t("--pl-color-fg-muted", fg);
  var font = t("--pl-font-sans", "ui-sans-serif, system-ui, sans-serif");
  var accent = t("--pl-color-accent", "#9b87f2");
  var axis = t("--pl-color-chart-axis", muted);
  var grid = t("--pl-color-chart-grid", t("--pl-color-border", "rgba(127,127,127,.25)"));
  var series = [], i;
  for (i = 1; i <= 8; i++) series.push(t("--pl-color-chart-series" + i, accent));

  // Sequential/diverging ramps (operator decision (a) on #571; designed tokens → #579).
  // Every stop a hex string — d3-color can't interpolate oklch().
  var s1 = series[0], s2 = series[1], s6 = series[5];
  var rampLow = mix(bg, s1, 0.15);
  var ramp = [rampLow, hexOf(s1)];
  var diverging = [hexOf(s6), mix(bg, muted, 0.5), hexOf(s2)];

  function guide(x) { x.labelColor = muted; x.titleColor = fg; x.labelFont = font; x.titleFont = font; return x; }

  return {
    background: bg,
    font: font,
    padding: 4,
    view: { stroke: null },
    axis: guide({
      domainColor: axis, tickColor: axis, gridColor: grid,
      labelFontSize: 11, titleFontSize: 12, titleFontWeight: 500,
      labelLimit: 180,
    }),
    legend: guide({
      labelFontSize: 11, titleFontSize: 12, titleFontWeight: 500,
      labelLimit: 200, symbolLimit: 0,
      orient: "bottom", direction: "horizontal", columns: 4,
    }),
    header: guide({ labelFontSize: 11, titleFontSize: 12 }),
    title: { color: fg, subtitleColor: muted, font: font, subtitleFont: font, anchor: "start", fontSize: 14, fontWeight: 600, offset: 12 },
    mark: { color: series[0] },
    text: { color: fg },
    rule: { color: axis },
    range: { category: series, ramp: ramp, heatmap: ramp, diverging: diverging },
  };
}

/**
 * The tooltip theme — "dark" | "light" from the luminance of `--pl-color-bg`, with no
 * canvas and no DOM. Parses `#rgb`/`#rrggbb`, `rgb()`/`rgba()` and `oklch()` (L a
 * percentage or 0–1). Unparseable input (or an absent ground) resolves to "dark".
 *
 * Self-contained like {@link buildVegaConfig}, so a sandbox can inline it too.
 *
 * @param {Record<string,string>} tok  flat `--pl-*` map
 * @returns {"dark"|"light"}
 */
export function vegaTooltipTheme(tok) {
  tok = tok || {};
  var v = tok["--pl-color-bg"];
  var bg = (typeof v === "string" && v.trim()) ? v.trim() : "#0a0a0c";
  var m, lum = null;
  if ((m = /^#([0-9a-f]{3})$/i.exec(bg))) {
    var h = m[1];
    lum = (0.299 * parseInt(h[0] + h[0], 16) + 0.587 * parseInt(h[1] + h[1], 16) + 0.114 * parseInt(h[2] + h[2], 16)) / 255;
  } else if ((m = /^#([0-9a-f]{6})$/i.exec(bg))) {
    var x = m[1];
    lum = (0.299 * parseInt(x.slice(0, 2), 16) + 0.587 * parseInt(x.slice(2, 4), 16) + 0.114 * parseInt(x.slice(4, 6), 16)) / 255;
  } else if ((m = /^rgba?\(\s*([0-9.]+)[,\s]+([0-9.]+)[,\s]+([0-9.]+)/i.exec(bg))) {
    lum = (0.299 * parseFloat(m[1]) + 0.587 * parseFloat(m[2]) + 0.114 * parseFloat(m[3])) / 255;
  } else if ((m = /^oklch\(\s*([0-9.]+)(%?)/i.exec(bg))) {
    // oklch L is already a perceptual lightness in 0–1 — use it directly as the proxy.
    lum = parseFloat(m[1]); if (m[2] === "%") lum = lum / 100;
  }
  if (lum === null || isNaN(lum)) return "dark";
  return lum <= 0.5 ? "dark" : "light";
}
