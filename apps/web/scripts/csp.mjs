// The console's Content-Security-Policy, as built output (`dist/csp.json`).
//
// The policy's directives live in ../csp-policy.json. What this adds is the sha256 of the one
// inline script the console creates: the streaming preview frame's (StreamingPreview.tsx). That
// frame is a `srcdoc` document, and a srcdoc document INHERITS its parent's policy — so without
// the hash, `script-src 'self'` would block the preview's own script. The server
// (src/operator_api/web.py) reads dist/csp.json and sends the header; the e2e mock server sends
// the same policy, enforced, so the whole e2e suite runs under it.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const here = (rel) => new URL(rel, import.meta.url);

/** The preview frame's inline script, exactly as StreamingPreview.tsx inlines it. */
export function previewScript() {
  const idiomorph = readFileSync(here("../src/artifacts/vendor/idiomorph.min.js"), "utf8");
  const receiver = readFileSync(here("../src/artifacts/previewReceiver.js"), "utf8");
  return joinPreviewScript(idiomorph, receiver);
}

/** The same join StreamingPreview.tsx applies (`PREVIEW_SCRIPT`; a unit test pins the two
 *  together). Any `</script` in the vendored lib is defused so it cannot close its host
 *  `<script>` early. */
export function joinPreviewScript(idiomorph, receiver) {
  return `${idiomorph.replace(/<\/(script)/gi, "<\\/$1")}\n${receiver}`;
}

export function sha256Source(text) {
  return `'sha256-${createHash("sha256").update(text, "utf8").digest("base64")}'`;
}

/** `{directives: {name: [sources]}}`, with the inline-script hash added to `script-src`. */
export function cspDocument() {
  const policy = JSON.parse(readFileSync(here("../csp-policy.json"), "utf8"));
  const directives = { ...policy.directives };
  directives["script-src"] = [...(directives["script-src"] || []), sha256Source(previewScript())];
  return { directives };
}

/** The header value for a directives map. */
export function cspHeader(directives) {
  return Object.entries(directives)
    .map(([name, sources]) => [name, ...sources].join(" "))
    .join("; ");
}

/** Vite plugin: write `csp.json` next to the built index.html. */
export function consoleCspPlugin() {
  let outDir = "dist";
  return {
    name: "protoagent-console-csp",
    configResolved(config) {
      outDir = join(config.root, config.build.outDir);
    },
    writeBundle() {
      writeFileSync(join(outDir, "csp.json"), JSON.stringify(cspDocument(), null, 2) + "\n");
    },
  };
}
