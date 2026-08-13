import { describe, it, expect, vi, beforeEach } from "vitest";

// #2179 P2, #2683 — confirmPublish posts a status note into the thread, same shape as
// exportChat.test.ts. "not configured" (the expected state until #2685's hosted service
// exists) must read as a warning note, not a danger one — it isn't a failure the operator
// caused.

const { publishChatSession } = vi.hoisted(() => ({
  publishChatSession: vi.fn(),
}));
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, publishChatSession } };
});

import { chatStore } from "./chat-store";
import { confirmPublish } from "./publishChat";

function makeSession(title: string): string {
  const session = chatStore.createSession();
  chatStore.renameSession(session.id, title);
  return session.id;
}

function messagesOf(sessionId: string) {
  return chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.messages ?? [];
}

beforeEach(() => {
  publishChatSession.mockReset();
});

describe("confirmPublish", () => {
  it("success note carries the public link, redaction count, and revoke token", async () => {
    publishChatSession.mockResolvedValue({
      published: true,
      public_url: "https://protolabs.studio/c/abc123",
      revoke_token: "rvk_xyz",
      expires_at: null,
      redactions: ["openai-key"],
      artifact_notes: [],
      message: "Published — https://protolabs.studio/c/abc123",
    });
    const sessionId = makeSession("Merck sync notes");

    await confirmPublish(sessionId, "Merck sync notes");

    const messages = messagesOf(sessionId);
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("system");
    expect(messages[0].noteTone).toBe("success");
    expect(messages[0].content).toContain("https://protolabs.studio/c/abc123");
    expect(messages[0].content).toContain("1 secret pattern(s) were redacted");
    expect(messages[0].content).toContain("rvk_xyz");
  });

  it("not-configured is a warning note, not a danger one — it's an expected state", async () => {
    publishChatSession.mockResolvedValue({
      published: false,
      reason: "not_configured",
      message: "Hosted publishing isn't configured on this instance yet.",
    });
    const sessionId = makeSession("t");

    await confirmPublish(sessionId);

    const messages = messagesOf(sessionId);
    expect(messages[0].noteTone).toBe("warning");
    expect(messages[0].content).toBe("Hosted publishing isn't configured on this instance yet.");
  });

  it("a real failure (quota/network/etc.) is a danger note", async () => {
    publishChatSession.mockResolvedValue({
      published: false,
      reason: "rejected",
      message: "Publish failed (rejected) — quota exceeded.",
    });
    const sessionId = makeSession("t");

    await confirmPublish(sessionId);

    expect(messagesOf(sessionId)[0].noteTone).toBe("danger");
  });

  it("a thrown/network error never rejects — it surfaces as a danger note", async () => {
    publishChatSession.mockRejectedValue(new Error("network down"));
    const sessionId = makeSession("t");

    await expect(confirmPublish(sessionId)).resolves.toBeUndefined();

    const messages = messagesOf(sessionId);
    expect(messages[0].noteTone).toBe("danger");
    expect(messages[0].content).toContain("network down");
  });

  it("omits the redaction/missing-artifact clauses when there's nothing to report", async () => {
    publishChatSession.mockResolvedValue({
      published: true,
      public_url: "https://protolabs.studio/c/xyz",
      revoke_token: "",
      expires_at: null,
      redactions: [],
      artifact_notes: [],
      message: "",
    });
    const sessionId = makeSession("t");

    await confirmPublish(sessionId);

    const content = messagesOf(sessionId)[0].content;
    expect(content).not.toContain("redacted");
    expect(content).not.toContain("artifact(s) noted");
    expect(content).not.toContain("Revoke token");
  });
});
