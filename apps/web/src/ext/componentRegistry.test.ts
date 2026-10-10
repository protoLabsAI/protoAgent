import { describe, expect, it } from "vitest";

import {
  dispatchLiveComponent,
  registerChatComponent,
  registeredChatComponents,
  resolveChatComponent,
} from "./componentRegistry";

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

// The fixed resolution order (ADR 0118 D5 / S12b): a TS-registered renderer wins, then a core
// built-in, then a plugin FRAME (a catalog `frame_url`), else unsupported — so a plugin ships a
// frame component with no console rebuild, yet a fork can still override any kind in TS.
describe("resolveChatComponent", () => {
  const builtin = () => null as never;
  const BUILTINS = { table: builtin };
  const frame = { frame_url: "/plugins/demo/widget", plugin: "demo" };

  it("a TS-registered renderer wins over a built-in of the same kind", () => {
    const reg = () => null as never;
    const off = registerChatComponent("table", reg);
    expect(resolveChatComponent("table", BUILTINS, frame)).toEqual({ via: "renderer", render: reg });
    off();
  });

  it("falls back to a built-in when nothing is registered", () => {
    expect(resolveChatComponent("table", BUILTINS, null)).toEqual({ via: "renderer", render: builtin });
  });

  it("resolves a catalog frame when neither a registered renderer nor a built-in claims the kind", () => {
    expect(resolveChatComponent("pl-demo", BUILTINS, frame)).toEqual({
      via: "frame",
      frameUrl: "/plugins/demo/widget",
      plugin: "demo",
    });
  });

  it("a TS-registered renderer STILL wins over a frame of the same kind", () => {
    const reg = () => null as never;
    const off = registerChatComponent("pl-demo", reg);
    expect(resolveChatComponent("pl-demo", BUILTINS, frame)).toEqual({ via: "renderer", render: reg });
    off();
  });

  it("is unsupported for an unknown kind with no frame — or a frame row whose frame_url is null", () => {
    expect(resolveChatComponent("nope", BUILTINS, null)).toEqual({ via: "unsupported" });
    expect(resolveChatComponent("nope", BUILTINS, { frame_url: null, plugin: "demo" })).toEqual({ via: "unsupported" });
  });
});
