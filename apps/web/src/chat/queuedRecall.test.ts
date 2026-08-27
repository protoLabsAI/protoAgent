import { describe, expect, it } from "vitest";

import { resolveComposerUp } from "./queuedRecall";

const q = (id: string, text: string) => ({ id, text });

describe("resolveComposerUp", () => {
  it("pulls the queued message when the composer is empty (#2837)", () => {
    expect(resolveComposerUp("", [q("s1", "actually, do X")])).toEqual({
      kind: "edit-queued",
      steerId: "s1",
      text: "actually, do X",
    });
  });

  it("pulls the NEWEST queued message first (LIFO, same order Escape peels)", () => {
    const action = resolveComposerUp("", [q("s1", "first"), q("s2", "second"), q("s3", "third")]);
    expect(action).toEqual({ kind: "edit-queued", steerId: "s3", text: "third" });
  });

  it("leaves ↑ to input history when nothing is queued", () => {
    expect(resolveComposerUp("", [])).toEqual({ kind: "history" });
  });

  it("leaves ↑ to input history while something is typed — a pull would clobber it", () => {
    expect(resolveComposerUp("half a thought", [q("s1", "queued")])).toEqual({ kind: "history" });
    // Including the press right after a pull: the recalled text is now the draft, so the
    // second ↑ walks history (stashing it) instead of silently eating the edit.
    expect(resolveComposerUp("actually, do X", [q("s2", "still queued")])).toEqual({ kind: "history" });
  });

  it("treats a whitespace-only draft as empty (a stray newline shouldn't block the pull)", () => {
    expect(resolveComposerUp("  \n ", [q("s1", "queued")])).toEqual({
      kind: "edit-queued",
      steerId: "s1",
      text: "queued",
    });
  });
});
