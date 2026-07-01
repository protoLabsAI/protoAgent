import { beforeEach, describe, expect, it } from "vitest";
import { loadPaletteThread } from "./paletteChatStore";

// jsdom path is "/" → the host (un-slugged) key.
const KEY = "protoagent.palette.chat";

describe("paletteChatStore load/sanitize", () => {
  beforeEach(() => window.localStorage.clear());

  it("preserves a streaming message that has a taskId (reconnectable) and settles one without", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        contextId: "palette-x",
        messages: [
          { role: "user", content: "hi" },
          // interrupted mid-turn but has a durable task id → kept streaming for self-heal
          { role: "assistant", content: "partial", status: "streaming", taskId: "task-123" },
          // stuck streaming with no task id → un-reconcilable, settle to done
          { role: "assistant", content: "orphan", status: "streaming" },
        ],
      }),
    );

    const t = loadPaletteThread();
    expect(t.contextId).toBe("palette-x");
    expect(t.messages[1].status).toBe("streaming");
    expect(t.messages[1].taskId).toBe("task-123");
    expect(t.messages[2].status).toBe("done");
  });

  it("drops malformed messages but keeps well-formed ones", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        contextId: "palette-y",
        messages: [{ role: "assistant", content: "ok" }, { role: "bogus" }, null, "nope"],
      }),
    );
    const t = loadPaletteThread();
    expect(t.messages).toHaveLength(1);
    expect(t.messages[0].content).toBe("ok");
  });

  it("returns a fresh thread on corrupt storage", () => {
    window.localStorage.setItem(KEY, "{not json");
    const t = loadPaletteThread();
    expect(t.contextId).toMatch(/^palette-/);
    expect(t.messages).toEqual([]);
  });
});
