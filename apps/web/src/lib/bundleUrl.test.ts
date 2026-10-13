import { describe, expect, it } from "vitest";

import { isValidBundleRef, parseBundleUrl } from "./bundleUrl";

// The New-agent "From a bundle URL" input check — mirrors the server's rules
// (operator_api/fleet_routes.py `_BUNDLE_URL_RE` + the installer's ref check) so a typo
// never costs a git fetch. The server re-validates; these are the shapes an operator pastes.

describe("parseBundleUrl", () => {
  it.each([
    ["https://github.com/protoLabsAI/analyst-archetype"],
    ["https://github.com/protoLabsAI/analyst-archetype.git"],
    ["https://github.com/protoLabsAI/analyst-archetype/"],
    ["https://gitlab.example.com:8443/group/sub/repo"],
    ["git@github.com:protoLabsAI/analyst-archetype.git"],
  ])("accepts %s", (url) => {
    expect(parseBundleUrl(`  ${url} `)).toEqual({ ok: true, url });
  });

  it.each([
    ["", "Paste the bundle's git URL."],
    ["http://github.com/a/b", "https://"],
    ["github.com/a/b", "isn't a git repository URL"],
    ["https://github.com/onlyowner", "isn't a git repository URL"],
    ["https://github.com/a/../b", "isn't a git repository URL"],
    ["https://github.com/a/b?tab=readme", "isn't a git repository URL"],
    ["file:///etc/passwd", "isn't a git repository URL"],
    ["/Users/me/bundle", "isn't a git repository URL"],
    ["--upload-pack=x", "isn't a git repository URL"],
  ])("rejects %j", (url, msg) => {
    const r = parseBundleUrl(url);
    expect(r.ok).toBe(false);
    expect(!r.ok && r.error).toContain(msg);
  });

  it("carries an explicit ref", () => {
    expect(parseBundleUrl("https://github.com/a/b", " v0.1.0 ")).toEqual({ ok: true, url: "https://github.com/a/b", ref: "v0.1.0" });
  });

  it.each([
    ["https://github.com/a/b/tree/v0.1.0", "v0.1.0"],
    ["https://github.com/a/b/tree/feature/x", "feature/x"],
    ["https://github.com/a/b/releases/tag/v2.0.0", "v2.0.0"],
    ["https://github.com/a/b/commit/0123abc", "0123abc"],
  ])("folds a GitHub page URL %s to repo + ref", (url, ref) => {
    expect(parseBundleUrl(url)).toEqual({ ok: true, url: "https://github.com/a/b", ref });
  });

  it("an explicit ref wins over one read from the page URL", () => {
    expect(parseBundleUrl("https://github.com/a/b/tree/main", "v1")).toEqual({ ok: true, url: "https://github.com/a/b", ref: "v1" });
  });

  it("rejects a ref that could reach git as an option or escape the path", () => {
    for (const ref of ["-x", "a..b", "v1 two", "../x"]) {
      expect(parseBundleUrl("https://github.com/a/b", ref).ok).toBe(false);
      expect(isValidBundleRef(ref)).toBe(false);
    }
  });
});
