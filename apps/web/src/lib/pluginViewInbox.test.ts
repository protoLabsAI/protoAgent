import { beforeEach, describe, expect, it } from "vitest";

import {
  onPluginViewMessage,
  postToPluginView,
  requeuePluginViewContext,
  resetPluginViewInbox,
  setPluginViewContext,
  takePluginViewMessages,
} from "./pluginViewInbox";

// Host → plugin-view deliveries (#3617): queued per view, latest-wins per type, drained once.

beforeEach(() => resetPluginViewInbox());

describe("pluginViewInbox", () => {
  it("queues per view, latest wins per type, drains once", () => {
    const seen: string[] = [];
    const off = onPluginViewMessage((k) => seen.push(k));
    postToPluginView("plugin:a:v", { type: "a:select", id: "1" });
    postToPluginView("plugin:a:v", { type: "a:other" });
    postToPluginView("plugin:a:v", { type: "a:select", id: "2" });
    postToPluginView("plugin:b:v", { type: "b:x" });
    expect(seen).toEqual(["plugin:a:v", "plugin:a:v", "plugin:a:v", "plugin:b:v"]);
    expect(takePluginViewMessages("plugin:a:v")).toEqual([{ type: "a:other" }, { type: "a:select", id: "2" }]);
    expect(takePluginViewMessages("plugin:a:v")).toEqual([]);
    expect(takePluginViewMessages("plugin:b:v")).toEqual([{ type: "b:x" }]);
    off();
  });

  it("refuses the host bridge's own protocol and non-plugin targets", () => {
    expect(postToPluginView("plugin:a:v", { type: "protoagent:init", token: "x" })).toBe(false);
    expect(postToPluginView("chat", { type: "a:select" })).toBe(false);
    expect(postToPluginView("plugin:a:v", { type: "" })).toBe(false);
    expect(takePluginViewMessages("plugin:a:v")).toEqual([]);
  });

  it("a sticky context is delivered now AND re-queued for every later page load (#4076)", () => {
    const seen: string[] = [];
    const off = onPluginViewMessage((k) => seen.push(k));
    expect(setPluginViewContext("plugin:a:v", { type: "a:context", session: "chat-1" })).toBe(true);
    expect(seen).toEqual(["plugin:a:v"]);
    expect(takePluginViewMessages("plugin:a:v")).toEqual([{ type: "a:context", session: "chat-1" }]);
    // The same value again is a no-op — no re-delivery to a page that already has it.
    setPluginViewContext("plugin:a:v", { type: "a:context", session: "chat-1" });
    expect(seen).toEqual(["plugin:a:v"]);
    expect(takePluginViewMessages("plugin:a:v")).toEqual([]);
    // The iframe reloads (its dock unmounted it): ready → the context is queued again, once,
    // beside anything else queued meanwhile.
    postToPluginView("plugin:a:v", { type: "a:select", id: "x" });
    requeuePluginViewContext("plugin:a:v");
    requeuePluginViewContext("plugin:a:v");
    expect(takePluginViewMessages("plugin:a:v")).toEqual([
      { type: "a:select", id: "x" },
      { type: "a:context", session: "chat-1" },
    ]);
    // A change is delivered at once and is what later loads get.
    setPluginViewContext("plugin:a:v", { type: "a:context", session: "chat-2" });
    expect(takePluginViewMessages("plugin:a:v")).toEqual([{ type: "a:context", session: "chat-2" }]);
    requeuePluginViewContext("plugin:a:v");
    expect(takePluginViewMessages("plugin:a:v")).toEqual([{ type: "a:context", session: "chat-2" }]);
    requeuePluginViewContext("plugin:b:v"); // no context for another view
    expect(takePluginViewMessages("plugin:b:v")).toEqual([]);
    off();
  });

  it("a sticky context obeys the same namespace rule", () => {
    expect(setPluginViewContext("plugin:a:v", { type: "protoagent:init", token: "x" })).toBe(false);
    expect(setPluginViewContext("chat", { type: "a:context" })).toBe(false);
    requeuePluginViewContext("plugin:a:v");
    expect(takePluginViewMessages("plugin:a:v")).toEqual([]);
  });
});
