// Copies @protolabsai/ui's no-build plugin-kit.css into public/_ds/ so Vite emits
// it to dist/_ds/plugin-kit.css, which the backend serves same-origin at
// /_ds/plugin-kit.css (operator_api/web.py). Plugin iframe pages <link> that one
// path instead of pinning a CDN copy — so every plugin view matches the console's
// installed design-system version automatically. See protoContent #176.
//
// Non-fatal: if the package/file isn't resolvable (e.g. @protolabsai/ui not yet
// bumped/installed to a version that ships the kit), it warns and skips rather than
// breaking the build — the /_ds route then 404s and plugins fall back to their own
// dark defaults until the dep is updated.

import { createRequire } from "node:module";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dest = resolve(here, "..", "public", "_ds", "plugin-kit.css");

try {
  const src = createRequire(import.meta.url).resolve("@protolabsai/ui/plugin-kit.css");
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(src, dest);
  console.log(`[copy-plugin-kit] ${src} -> public/_ds/plugin-kit.css`);
} catch (err) {
  console.warn(
    `[copy-plugin-kit] skipped — could not resolve @protolabsai/ui/plugin-kit.css ` +
      `(bump @protolabsai/ui to >=0.19 and reinstall). ${err?.message ?? err}`,
  );
}
