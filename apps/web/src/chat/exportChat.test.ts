import { describe, it, expect, vi, beforeEach } from "vitest";

// #2197 — the export note must name the file it wrote (the browser picks the destination,
// so the note is the only place the operator learns the filename), and when the surface
// blocks programmatic downloads the note must say THAT instead of claiming success.

// Partial mocks: only the seams exportChatToFile crosses are swapped — api.exportChatSession
// (the server round-trip) and downloadTextFile (the browser gesture). safeFilename and the
// real chatStore stay live so the filename in the note is the one actually derived.
const { exportChatSession, downloadTextFile } = vi.hoisted(() => ({
  exportChatSession: vi.fn(),
  downloadTextFile: vi.fn(),
}));
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, exportChatSession } };
});
vi.mock("../lib/download", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/download")>();
  return { ...actual, downloadTextFile };
});

import { chatStore } from "./chat-store";
import { exportChatToFile } from "./exportChat";

const FOUND = {
  found: true,
  markdown: "# thread\n",
  message_count: 9,
  redactions: [] as string[],
  reason: "",
  message: "",
};

function makeSession(title: string): string {
  const session = chatStore.createSession();
  chatStore.renameSession(session.id, title);
  return session.id;
}

function messagesOf(sessionId: string) {
  return chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.messages ?? [];
}

beforeEach(() => {
  exportChatSession.mockReset();
  downloadTextFile.mockReset();
});

describe("exportChatToFile — the note names the destination file (#2197)", () => {
  it("success note carries the filename the download was written under", async () => {
    exportChatSession.mockResolvedValue({ ...FOUND });
    downloadTextFile.mockReturnValue(true);
    const sessionId = makeSession("Merck sync notes");

    await exportChatToFile(sessionId);

    expect(downloadTextFile).toHaveBeenCalledWith("Merck sync notes.md", "# thread\n");
    const messages = messagesOf(sessionId);
    expect(messages).toHaveLength(1);
    expect(messages[0].noteTone).toBe("success");
    expect(messages[0].content).toContain(
      "Exported 9 message(s) → `Merck sync notes.md` (check your browser downloads).",
    );
  });

  it("keeps the redaction warning suffix alongside the filename", async () => {
    exportChatSession.mockResolvedValue({ ...FOUND, redactions: ["OPENAI_API_KEY"] });
    downloadTextFile.mockReturnValue(true);
    const sessionId = makeSession("Prod incident");

    await exportChatToFile(sessionId);

    const messages = messagesOf(sessionId);
    expect(messages).toHaveLength(1);
    expect(messages[0].noteTone).toBe("warning");
    expect(messages[0].content).toContain("`Prod incident.md`");
    expect(messages[0].content).toContain("**1 secret pattern(s) were redacted**");
  });
});

describe("exportChatToFile — blocked download reports, never claims success (#2197)", () => {
  it("posts ONLY the danger note when downloadTextFile returns false", async () => {
    exportChatSession.mockResolvedValue({ ...FOUND });
    downloadTextFile.mockReturnValue(false);
    const sessionId = makeSession("Blocked surface");

    await exportChatToFile(sessionId);

    const messages = messagesOf(sessionId);
    expect(messages).toHaveLength(1);
    expect(messages[0].noteTone).toBe("danger");
    expect(messages[0].content).toBe("Export blocked on this surface — no file was written");
    expect(messages.some((m) => m.content.includes("Exported"))).toBe(false);
  });
});
