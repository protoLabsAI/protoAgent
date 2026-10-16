import { describe, expect, it, vi } from "vitest";

import {
  COMPOSE_MIN_INTERVAL_MS,
  createPluginChatComposer,
  MAX_COMPOSE_BYTES,
  parsePluginChatCompose,
  type ComposeDeps,
} from "./pluginChatCompose";

const msg = (text: unknown, extra: Record<string, unknown> = {}) => ({ type: "protoagent:chat:compose", text, ...extra });

describe("parsePluginChatCompose", () => {
  it("accepts plain text and trims it", () => {
    expect(parsePluginChatCompose(msg("  Explain the Login button \n"))).toEqual({ text: "Explain the Login button" });
  });

  it("rejects the wrong type, non-objects, non-string and blank text", () => {
    expect(parsePluginChatCompose(null)).toBeNull();
    expect(parsePluginChatCompose("protoagent:chat:compose")).toBeNull();
    expect(parsePluginChatCompose({ type: "protoagent:chat:send", text: "hi" })).toBeNull();
    expect(parsePluginChatCompose(msg(42))).toBeNull();
    expect(parsePluginChatCompose(msg({ html: "<b>x</b>" }))).toBeNull();
    expect(parsePluginChatCompose(msg("   \n\t "))).toBeNull();
  });

  it("strips control characters and normalizes line endings, keeping tabs and newlines", () => {
    expect(parsePluginChatCompose(msg("a\r\nb\rc\td\u0000e\u001bf\u007fg\u009bh"))).toEqual({ text: "a\nb\nc\tdefgh" });
  });

  it("caps at MAX_COMPOSE_BYTES in UTF-8 bytes — refused, never truncated", () => {
    expect(parsePluginChatCompose(msg("x".repeat(MAX_COMPOSE_BYTES)))?.text.length).toBe(MAX_COMPOSE_BYTES);
    expect(parsePluginChatCompose(msg("x".repeat(MAX_COMPOSE_BYTES + 1)))).toBeNull();
    // 3-byte chars: 1366 × 3 = 4098 bytes, under 4096 characters but over the byte cap.
    expect(parsePluginChatCompose(msg("€".repeat(1366)))).toBeNull();
  });

  it("drops send/session/agent fields — the result is text only", () => {
    expect(parsePluginChatCompose(msg("hi", { send: true, sessionId: "s9", agent: "other" }))).toEqual({ text: "hi" });
  });
});

function deps(over: Partial<ComposeDeps> = {}) {
  let t = 10_000;
  const d = {
    ready: vi.fn((act: () => void) => act()),
    prefill: vi.fn(() => true),
    notify: vi.fn(),
    now: () => t,
    ...over,
  };
  return { d, advance: (ms: number) => (t += ms) };
}

describe("createPluginChatComposer", () => {
  it("prefills through the chat seam and names the plugin in the toast", () => {
    const { d } = deps();
    const compose = createPluginChatComposer("Atlas (atlas)", d);
    expect(compose(msg("Explain this"))).toBe(true);
    expect(d.prefill).toHaveBeenCalledWith("Explain this");
    expect(d.notify).toHaveBeenCalledWith(expect.objectContaining({ tone: "info", message: expect.stringContaining("Atlas (atlas)") }));
  });

  it("ignores a malformed message without raising chat", () => {
    const { d } = deps();
    const compose = createPluginChatComposer("Atlas", d);
    expect(compose(msg(""))).toBe(false);
    expect(d.ready).not.toHaveBeenCalled();
  });

  it("throttles a view that composes in a loop", () => {
    const prefill = vi.fn((_text: string) => true);
    const { d, advance } = deps({ prefill });
    const compose = createPluginChatComposer("Atlas", d);
    expect(compose(msg("one"))).toBe(true);
    expect(compose(msg("two"))).toBe(false);
    advance(COMPOSE_MIN_INTERVAL_MS);
    expect(compose(msg("three"))).toBe(true);
    expect(prefill.mock.calls.map((c) => c[0])).toEqual(["one", "three"]);
  });

  it("warns when no composer ever appears", () => {
    const { d } = deps({ ready: vi.fn((_act: () => void, giveUp: () => void) => giveUp()) });
    const compose = createPluginChatComposer("Atlas", d);
    compose(msg("hi"));
    expect(d.prefill).not.toHaveBeenCalled();
    expect(d.notify).toHaveBeenCalledWith(expect.objectContaining({ tone: "warning" }));
  });

  it("warns when the seam vanished between raise and write", () => {
    const { d } = deps({ prefill: vi.fn(() => false) });
    createPluginChatComposer("Atlas", d)(msg("hi"));
    expect(d.notify).toHaveBeenCalledWith(expect.objectContaining({ tone: "warning" }));
  });
});
