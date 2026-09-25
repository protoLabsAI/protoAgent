import { describe, expect, it } from "vitest";

import { dispatchLiveComponent, registerChatComponent, registeredChatComponents } from "./componentRegistry";

// The fork/plugin seam for inline chat-component renderers (#1323) — add a new kind, override
// a built-in (last-wins), and unregister.

const render = () => null as never; // a stand-in renderer (identity not exercised here)

describe("registerChatComponent", () => {
  it("registers a renderer by kind and exposes it", () => {
    const off = registerChatComponent("badge", render);
    expect(registeredChatComponents().badge).toBe(render);
    off();
    expect(registeredChatComponents().badge).toBeUndefined();
  });

  it("last registration of a kind wins (a fork can re-skin a built-in)", () => {
    const a = () => null as never;
    const b = () => null as never;
    const offA = registerChatComponent("table", a);
    expect(registeredChatComponents().table).toBe(a);
    const offB = registerChatComponent("table", b); // override
    expect(registeredChatComponents().table).toBe(b);
    offB();
    offA();
    expect(registeredChatComponents().table).toBeUndefined();
  });

  it("ignores a blank name or a non-function renderer", () => {
    registerChatComponent("", render);
    // @ts-expect-error — guarding the runtime path
    registerChatComponent("bad", null);
    expect(registeredChatComponents()[""]).toBeUndefined();
    expect(registeredChatComponents().bad).toBeUndefined();
  });
});

describe("live component hooks (#3617)", () => {
  it("fire only for their kind, contain a throw, and unregister with the renderer", () => {
    const calls: Array<[string, string | undefined]> = [];
    const off = registerChatComponent("pin", render, {
      onLive: (spec, ctx) => calls.push([String(spec.props.id), ctx.sessionId]),
    });
    dispatchLiveComponent({ component: "pin", props: { id: "x" } }, "s1");
    dispatchLiveComponent({ component: "other", props: { id: "y" } }, "s1");
    expect(calls).toEqual([["x", "s1"]]);
    const offBoom = registerChatComponent("boom", render, {
      onLive: () => {
        throw new Error("plugin bug");
      },
    });
    expect(() => dispatchLiveComponent({ component: "boom", props: {} })).not.toThrow();
    offBoom();
    off();
    dispatchLiveComponent({ component: "pin", props: { id: "z" } }, "s1");
    expect(calls).toEqual([["x", "s1"]]);
  });

  it("a re-registration without a hook drops the old hook", () => {
    const calls: string[] = [];
    registerChatComponent("pin2", render, { onLive: () => calls.push("old") });
    const off = registerChatComponent("pin2", render);
    dispatchLiveComponent({ component: "pin2", props: {} });
    expect(calls).toEqual([]);
    off();
  });
});
