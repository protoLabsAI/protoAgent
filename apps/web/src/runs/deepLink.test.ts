import { afterEach, describe, expect, it, vi } from "vitest";

import { parseRunDeepLink, runDeepLinkUrl, stripRunDeepLink } from "../lib/viewDeepLink";
import { applyRunDeepLink } from "./deepLink";
import { closeRunInspector, useRunInspector } from "./store";

// `?run=<id>` (#4726): a link that opens the run inspector at boot, then leaves the bar.
afterEach(() => closeRunInspector());

describe("run deep link", () => {
  it("parses the id and refuses a malformed one", () => {
    expect(parseRunDeepLink("?run=7f3a9c1e")).toBe("7f3a9c1e");
    expect(parseRunDeepLink("?run=board%3A42&__apiPort=7870")).toBe("board:42");
    expect(parseRunDeepLink("?run=a%20b")).toBeNull();
    expect(parseRunDeepLink("?run=")).toBeNull();
    expect(parseRunDeepLink("?plugin=x")).toBeNull();
  });

  it("strips only the run key", () => {
    expect(stripRunDeepLink("?run=e1&__apiPort=7870")).toBe("?__apiPort=7870");
    expect(stripRunDeepLink("?run=e1")).toBe("");
  });

  it("builds a link on this console's own path, dropping any other deep link", () => {
    const loc = { origin: "https://hub.example", pathname: "/app/agent/ava/", search: "?plugin=orgchart&__apiPort=7870" };
    expect(runDeepLinkUrl("board:42", loc)).toBe("https://hub.example/app/agent/ava/?__apiPort=7870&run=board%3A42");
  });

  it("opens the drawer at boot and strips the key from the address bar", () => {
    const replaceState = vi.fn();
    const loc = { pathname: "/app/agent/ava/", search: "?run=task-1&x=1", hash: "#h" } as Location;
    expect(applyRunDeepLink(loc, { state: null, replaceState } as unknown as History)).toBe("task-1");
    expect(useRunInspector.getState().runId).toBe("task-1");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/app/agent/ava/?x=1#h");
  });

  it("strips a malformed link without opening anything, and leaves a link-less URL alone", () => {
    const replaceState = vi.fn();
    const hist = { state: null, replaceState } as unknown as History;
    expect(applyRunDeepLink({ pathname: "/", search: "?run=a%20b", hash: "" } as Location, hist)).toBeNull();
    expect(replaceState).toHaveBeenCalledWith(null, "", "/");
    expect(useRunInspector.getState().runId).toBeNull();
    replaceState.mockClear();
    expect(applyRunDeepLink({ pathname: "/", search: "?plugin=x", hash: "" } as Location, hist)).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
