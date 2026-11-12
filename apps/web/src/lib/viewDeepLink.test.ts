// @vitest-environment node
import { describe, expect, it } from "vitest";

import {
  MAX_VIEW_QUERY,
  parseViewDeepLink,
  resolveViewDeepLink,
  stripViewDeepLink,
  withViewQuery,
} from "./viewDeepLink";

describe("parseViewDeepLink", () => {
  it("is null without a plugin param", () => {
    expect(parseViewDeepLink("")).toBeNull();
    expect(parseViewDeepLink("?__apiPort=7881&view=x")).toBeNull();
  });

  it("reads plugin, view and the forwarded query", () => {
    expect(parseViewDeepLink("?plugin=inventory&view=view&vp=bin%3DB07")).toEqual({
      plugin: "inventory",
      view: "view",
      query: "bin=B07",
    });
  });

  it("view is optional (the plugin's first view)", () => {
    expect(parseViewDeepLink("?plugin=inventory")).toEqual({ plugin: "inventory", view: "", query: "" });
  });

  it("re-serializes vp so it can only add query data, never a path or fragment", () => {
    const out = parseViewDeepLink(`?plugin=x&vp=${encodeURIComponent("a=1&b=/../../api/secrets?c=#d")}`);
    const q = (out as { query: string }).query;
    expect(q.startsWith("a=1&b=")).toBe(true);
    expect(q).not.toMatch(/[/?#]/);
    expect(new URLSearchParams(q).get("b")).toBe("/../../api/secrets?c=#d");
  });

  it("accepts a leading ? inside vp", () => {
    expect(parseViewDeepLink(`?plugin=x&vp=${encodeURIComponent("?bin=B01")}`)).toMatchObject({ query: "bin=B01" });
  });

  it("refuses ids that are not plugin/view ids", () => {
    expect(parseViewDeepLink("?plugin=../etc")).toHaveProperty("error");
    expect(parseViewDeepLink("?plugin=")).toHaveProperty("error");
    expect(parseViewDeepLink("?plugin=https://evil.example")).toHaveProperty("error");
    expect(parseViewDeepLink("?plugin=ok&view=a%20b")).toHaveProperty("error");
    expect(parseViewDeepLink("?plugin=ok&view=a%0Ab")).toHaveProperty("error");
  });

  it("accepts any manifest-legal view id, e.g. with a colon", () => {
    expect(parseViewDeepLink("?plugin=sales&view=sales%3Adaily")).toMatchObject({ view: "sales:daily" });
  });

  it("refuses an oversized forwarded query", () => {
    const big = "k=" + "x".repeat(MAX_VIEW_QUERY);
    expect(parseViewDeepLink(`?plugin=x&vp=${encodeURIComponent(big)}`)).toHaveProperty("error");
  });
});

describe("stripViewDeepLink", () => {
  it("removes only the deep-link keys", () => {
    expect(stripViewDeepLink("?__apiPort=7881&plugin=x&view=y&vp=a%3D1&flag=on")).toBe("?__apiPort=7881&flag=on");
    expect(stripViewDeepLink("?plugin=x&vp=a%3D1")).toBe("");
    expect(stripViewDeepLink("")).toBe("");
  });
});

describe("withViewQuery", () => {
  it("appends with ? or & and keeps a fragment last", () => {
    expect(withViewQuery("/plugins/x/view", "bin=B07")).toBe("/plugins/x/view?bin=B07");
    expect(withViewQuery("/plugins/x/board?tab=open", "bin=B07")).toBe("/plugins/x/board?tab=open&bin=B07");
    expect(withViewQuery("/plugins/x/view#top", "a=1")).toBe("/plugins/x/view?a=1#top");
    expect(withViewQuery("/plugins/x/view", "")).toBe("/plugins/x/view");
    expect(withViewQuery("/plugins/x/view", undefined)).toBe("/plugins/x/view");
  });
});

describe("resolveViewDeepLink", () => {
  const views = [{ key: "plugin:boardy:board" }, { key: "plugin:boardy:stats" }, { key: "plugin:other:main" }];
  it("finds the named view or the plugin's first", () => {
    expect(resolveViewDeepLink({ plugin: "boardy", view: "stats", query: "" }, views)?.key).toBe("plugin:boardy:stats");
    expect(resolveViewDeepLink({ plugin: "boardy", view: "", query: "" }, views)?.key).toBe("plugin:boardy:board");
  });
  it("is null for an unknown plugin or view", () => {
    expect(resolveViewDeepLink({ plugin: "nope", view: "", query: "" }, views)).toBeNull();
    expect(resolveViewDeepLink({ plugin: "boardy", view: "nope", query: "" }, views)).toBeNull();
  });
  it("does not match a plugin id that is a prefix of another", () => {
    expect(resolveViewDeepLink({ plugin: "board", view: "", query: "" }, [{ key: "plugin:boardy:board" }])).toBeNull();
  });
});
