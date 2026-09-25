import { beforeEach, describe, expect, it } from "vitest";

import { onPluginViewMessage, postToPluginView, resetPluginViewInbox, takePluginViewMessages } from "./pluginViewInbox";

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
});
