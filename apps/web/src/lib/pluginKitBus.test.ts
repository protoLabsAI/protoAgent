import { describe, expect, it, vi } from "vitest";
import { createPluginEventRelay, parseSubscribe } from "./pluginEventRelay";

import kitSource from "@protolabsai/ui/plugin-kit.js?raw";
import copiedKit from "../../public/_ds/plugin-kit.js?raw";
import chartSource from "../../../../plugins/orgchart/view/orgchart.js?raw";

describe("published plugin kit and org chart bus integration", () => {
  it("exports the bus helpers in the artifact copied to public/_ds", () => {
    expect(copiedKit).toBe(kitSource);
    for (const helper of ["subscribe", "publish", "onEvent"]) {
      expect(kitSource).toMatch(new RegExp(`export function ${helper}\\b`));
    }
  });

  it("delivers replay and live delegation frames through the real kit and chart subscription", () => {
    // Evaluate the published no-build module in an isolated iframe-like window. Only
    // remove ESM export keywords; exercise the kit's actual pattern matching/dispatch.
    const frame = new EventTarget() as EventTarget & { parent: { postMessage: (msg: unknown) => void } };
    const relay = createPluginEventRelay({
      post: (event) => frame.dispatchEvent(new MessageEvent("message", {
        data: { type: "protoagent:event", ...event },
      })),
      replaySince: () => [{ topic: "delegation.started", data: { id: "run" }, seq: 1 }],
    });
    frame.parent = { postMessage: (msg) => {
      const request = parseSubscribe(msg);
      if (request) {
        expect(request.background).toBe(true);
        relay.subscribe(request);
      }
    } };
    const kit = new Function("window", "sessionStorage", `${kitSource.replace(/^export /gm, "")}\nreturn window.protoPluginView;`)(
      frame, { getItem: () => "0", setItem: vi.fn() },
    );
    const onEvent = vi.fn();
    const setLive = vi.fn();
    const subscribe = chartSource.match(/function subscribe\(\) \{([\s\S]*?)\n\}/)![1];
    new Function("kit", "onEvent", "setLive", subscribe)(kit, onEvent, setLive);
    relay.deliver("delegation.progress", { id: "run", step: 2 }, 2);
    relay.deliver("delegation.finished", { id: "run", outcome: "ok" }, 3);
    relay.deliver("delegation.nested.progress", { id: "nested" }, 4);
    relay.deliver("unrelated.event", {}, 5);
    relay.deliver("delegation.finished", { id: "run" }, 3); // duplicate
    expect(onEvent.mock.calls).toEqual([
      ["delegation.started", { id: "run" }],
      ["delegation.progress", { id: "run", step: 2 }],
      ["delegation.finished", { id: "run", outcome: "ok" }],
      ["delegation.nested.progress", { id: "nested" }],
    ]);
    expect(setLive.mock.calls).toEqual([["polling"]]);
  });
});
