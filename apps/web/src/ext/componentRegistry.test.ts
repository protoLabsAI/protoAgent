import { describe, expect, it } from "vitest";

import { registerChatComponent, registeredChatComponents } from "./componentRegistry";

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
