import { describe, expect, it } from "vitest";

import { isSameOriginViewPath, viewPathsAreSameOrigin } from "./pluginViewPath";
import { qrImageSrc } from "../settings/DevicesPanel";

describe("isSameOriginViewPath — a view path that can only reach the agent's own server", () => {
  it("accepts the shapes manifests declare", () => {
    for (const path of [
      "/plugins/boardy/board",
      "/plugins/notes/view?tab=open&at=12:30",
      "/api/plugins/boardy/board",
      "/plugins/artifact/embed?embed=a%3Ab&v=3",
      "plugins/docs/view",
    ]) {
      expect(isSameOriginViewPath(path), path).toBe(true);
    }
  });

  it("refuses anything carrying a scheme, host or port — or shaped to become one", () => {
    for (const path of [
      "https://evil.example/v",
      "http://127.0.0.1:9999/v",
      "//evil.example/v",
      " //evil.example/v",
      "\u00a0//evil.example/v",
      "/plugins/x/view ",
      "/\\evil.example/v",
      "\\\\evil.example\\v",
      "javascript:alert(1)",
      "data:text/html,<script>1</script>",
      "/plugins/x/..\t/..\t/api/config",
      "evil.example:8443/v",
      "",
      "   ",
      undefined,
      42,
    ]) {
      expect(isSameOriginViewPath(path), String(path)).toBe(false);
    }
  });

  it("checks a view's sub-tabs too", () => {
    expect(viewPathsAreSameOrigin({ path: "/plugins/a/v", tabs: [{ path: "/plugins/a/t" }] })).toBe(true);
    expect(viewPathsAreSameOrigin({ path: "/plugins/a/v", tabs: [{ path: "https://evil.example/t" }] })).toBe(false);
    expect(viewPathsAreSameOrigin({ path: "https://evil.example/v" })).toBe(false);
  });
});

describe("qrImageSrc — a pairing QR is shown as an image, never injected as markup", () => {
  it("wraps whatever the server sent in an inert data: image", () => {
    const hostile = `<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg><img src=x onerror=alert(2)>`;
    const src = qrImageSrc(hostile);
    expect(src.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    expect(src).not.toContain("<");
    expect(decodeURIComponent(src.split(",", 2)[1])).toBe(hostile);
  });
});
