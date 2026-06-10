// Copies @protolabsai/ui's no-build plugin-kit (CSS + JS) into public/_ds/ so
// Vite emits them to dist/_ds/, which the backend serves same-origin at
// /_ds/plugin-kit.css and /_ds/plugin-kit.js (operator_api/web.py). Plugin iframe
// pages <link>/<script> those two paths instead of pinning a CDN copy — so every
// plugin view matches the console's installed design-system version automatically.
// See protoContent #176.
//
// Non-fatal: if a package/file isn't resolvable (e.g. @protolabsai/ui not yet
// bumped/installed to a version that ships the kit), it warns and skips that file
// rather than breaking the build — the /_ds route then 404s and plugins fall back
// to their own dark defaults until the dep is updated.

import { createRequire } from "node:module";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// Each asset: the package export to resolve, and the public/_ds/ filename to write.
const assets = [
  { spec: "@protolabsai/ui/plugin-kit.css", out: "plugin-kit.css" },
  { spec: "@protolabsai/ui/plugin-kit.js", out: "plugin-kit.js" },
];

for (const { spec, out } of assets) {
  const dest = resolve(here, "..", "public", "_ds", out);
  try {
    const src = require.resolve(spec);
    mkdirSync(dirname(dest), { recursive: true });
    copyFileSync(src, dest);
    console.log(`[copy-plugin-kit] ${src} -> public/_ds/${out}`);
  } catch (err) {
    console.warn(
      `[copy-plugin-kit] skipped — could not resolve ${spec} ` +
        `(bump @protolabsai/ui to >=0.19 and reinstall). ${err?.message ?? err}`,
    );
  }
}
