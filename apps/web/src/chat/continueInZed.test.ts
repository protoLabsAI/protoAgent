import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import { continueInZed, type ContinueInZedDeps, type HandoffBody } from "./continueInZed";

function harness(over: Partial<ContinueInZedDeps> = {}) {
  const posted: HandoffBody[] = [];
  const opened: string[] = [];
  const toasts: { tone: string; title: string; message: string }[] = [];
  let rootsCalls = 0;
  const deps: ContinueInZedDeps = {
    post: async (b) => {
      posted.push(b);
      return { id: "ho-1", expires_at: "", root: null };
    },
    roots: async () => {
      rootsCalls++;
      return { repo: "/Users/me/dev/nava/repo" };
    },
    navigate: (h) => opened.push(h),
    toast: (t) => toasts.push(t),
    ...over,
  };
  return { deps, posted, opened, toasts, rootsCalls: () => rootsCalls };
}

describe("continueInZed", () => {
  it("offers the chat for the code pane's project and opens its current file in Zed", async () => {
    const h = harness();
    const href = await continueInZed(
      {
        sessionId: "chat-1",
        title: "  Router bug ",
        current: { project: "repo", path: "src/router.py", line: 42, source: "link", sessionId: "chat-1" },
        agentName: "navaEngineer",
      },
      h.deps,
    );
    expect(h.posted).toEqual([{ session_id: "chat-1", title: "Router bug", project: "repo", path: "src/router.py", line: 42 }]);
    expect(href).toBe("zed://file/Users/me/dev/nava/repo/src/router.py:42");
    expect(h.opened).toEqual([href]);
    expect(h.toasts).toEqual([
      {
        tone: "success",
        title: "Ready to continue in Zed",
        message: "Start a navaEngineer thread in Zed within 2 minutes to continue this chat.",
      },
    ]);
  });

  it("with no file open: omits the project, launches nothing, and says to switch to Zed", async () => {
    const h = harness();
    const href = await continueInZed({ sessionId: "chat-2", current: null, agentName: "protoAgent" }, h.deps);
    expect(h.posted).toEqual([{ session_id: "chat-2" }]);
    expect(href).toBeNull();
    expect(h.opened).toEqual([]);
    expect(h.rootsCalls()).toBe(0);
    expect(h.toasts[0].message).toBe("Switch to Zed and start a protoAgent thread within 2 minutes to continue this chat.");
  });

  it("a 404 (nothing sent yet) toasts an error and opens nothing", async () => {
    const h = harness({
      post: async () => {
        throw new ApiError(404, "unknown session", "not_found");
      },
    });
    const href = await continueInZed(
      { sessionId: "chat-new", current: { project: "repo", path: "a.ts", source: "link", sessionId: "chat-new" }, agentName: "x" },
      h.deps,
    );
    expect(href).toBeNull();
    expect(h.opened).toEqual([]);
    expect(h.toasts).toEqual([{ tone: "error", title: "Nothing to continue yet", message: "Send a message in this chat first." }]);
  });

  it("other failures surface the server's reason", async () => {
    const h = harness({
      post: async () => {
        throw new ApiError(400, "not a registered project: 'gone'", "unknown_project");
      },
    });
    await continueInZed({ sessionId: "c", current: { project: "gone", path: "a.ts", source: "link", sessionId: "c" }, agentName: "x" }, h.deps);
    expect(h.toasts[0]).toMatchObject({ tone: "error", title: "Couldn't hand off to Zed", message: "not a registered project: 'gone'" });
  });

  it("keeps the hand-off when the roots lookup fails — only the jump is lost", async () => {
    const h = harness({
      roots: async () => {
        throw new Error("offline");
      },
    });
    const href = await continueInZed(
      { sessionId: "c", current: { project: "repo", path: "a.ts", source: "link", sessionId: "c" }, agentName: "Nava" },
      h.deps,
    );
    expect(href).toBeNull();
    expect(h.posted).toHaveLength(1);
    expect(h.toasts[0]).toMatchObject({ tone: "success" });
    expect(h.toasts[0].message).toContain("Switch to Zed");
  });

  it("ignores the pane's file when it was opened from ANOTHER chat (or from no known chat)", async () => {
    for (const origin of ["chat-other", undefined]) {
      const h = harness();
      const href = await continueInZed(
        {
          sessionId: "chat-empty",
          current: { project: "repo", path: "src/router.py", line: 3, source: "link", sessionId: origin },
          agentName: "Nava",
        },
        h.deps,
      );
      expect(h.posted).toEqual([{ session_id: "chat-empty" }]);
      expect(href).toBeNull();
      expect(h.opened).toEqual([]);
      expect(h.rootsCalls()).toBe(0);
    }
  });
});
