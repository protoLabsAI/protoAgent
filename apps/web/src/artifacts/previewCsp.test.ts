// @vitest-environment node
import { describe, expect, it } from "vitest";

import { cspDocument, inlineScriptHashes, previewScript, sha256Source } from "../../scripts/csp.mjs";
import { PREVIEW_SCRIPT } from "./StreamingPreview";

describe("the console CSP allows exactly the streaming preview's inline script", () => {
  it("hashes the same bytes the component inlines", async () => {
    // A srcdoc frame inherits the console's policy, so script-src must carry this exact hash.
    expect(previewScript()).toBe(PREVIEW_SCRIPT);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(PREVIEW_SCRIPT));
    const hash = `'sha256-${btoa(String.fromCharCode(...new Uint8Array(digest)))}'`;
    expect(sha256Source(PREVIEW_SCRIPT)).toBe(hash);
    expect(cspDocument().directives["script-src"]).toContain(hash);
  });

  it("keeps the receiver's message types and ids in step with the component", () => {
    for (const id of ["proto-preview:height", "proto-preview:morph", "proto-preview:style", "proto-preview-root", "proto-preview-kit", "proto-preview-theme"]) {
      expect(PREVIEW_SCRIPT, id).toContain(`"${id}"`);
    }
  });

  it("hashes the inline boot-splash script, and only classic inline scripts", () => {
    // The built index.html carries the splash script (bd-aawcj); its sha256 must be allowed, while
    // the module entry (src=) and external scripts stay covered by 'self'.
    const html = [
      `<script>var a = 1;</script>`,
      `<script type="module" src="/app/assets/index-abc.js"></script>`,
      `<script src="/x.js"></script>`,
      `<script>  </script>`,
    ].join("\n");
    expect(inlineScriptHashes(html)).toEqual([sha256Source("var a = 1;")]);
  });

  it("never lets a page script run without the hash, and never allows eval", () => {
    const d = cspDocument().directives;
    expect(d["script-src"]).not.toContain("'unsafe-inline'");
    expect(d["script-src"]).not.toContain("'unsafe-eval'");
    expect(d["object-src"]).toEqual(["'none'"]);
    expect(d["frame-ancestors"]).toEqual(["'self'"]);
  });
});
