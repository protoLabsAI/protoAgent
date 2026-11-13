// The streaming preview frame's receiver (ADR 0118 D3). Inlined, after the vendored Idiomorph,
// as the frame's ONE script — see StreamingPreview.tsx. Plain text on purpose: the console's
// Content-Security-Policy allows this exact script by its sha256 (a srcdoc frame inherits the
// console's policy), and the hash is computed from this file at build time (scripts/csp.mjs).
// The message types and element ids below must match the constants in StreamingPreview.tsx
// (a unit test checks them).
(function () {
  function report() {
    try {
      var h = Math.ceil(document.documentElement.scrollHeight || document.body.scrollHeight || 0);
      parent.postMessage({ type: "proto-preview:height", height: h }, "*");
    } catch (_) {}
  }
  function render(html) {
    var root = document.getElementById("proto-preview-root");
    if (!root) return;
    try {
      Idiomorph.morph(root, html, { morphStyle: "innerHTML" });
    } catch (_) {
      root.innerHTML = "";
    }
    report();
  }
  // A host style post: fill one of the two HOST-owned style elements (kit / theme) by id, never
  // any other node, and re-stamp the scheme on <html>, which the theme rules key off.
  function restyle(d) {
    if (d.id !== "proto-preview-kit" && d.id !== "proto-preview-theme") return;
    var el = document.getElementById(d.id);
    if (el) el.textContent = d.css;
    if (d.scheme === "light" || d.scheme === "dark") document.documentElement.setAttribute("data-theme", d.scheme);
    report();
  }
  window.addEventListener("message", function (ev) {
    if (ev.source !== parent) return;
    var d = ev.data;
    if (d && d.type === "proto-preview:style" && typeof d.css === "string") {
      restyle(d);
      return;
    }
    if (!d || d.type !== "proto-preview:morph" || typeof d.html !== "string") return;
    render(d.html);
  });
})();
