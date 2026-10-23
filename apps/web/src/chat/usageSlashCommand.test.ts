// /usage — chat-native subscription usage. Mocks only the http `request` (importOriginal
// keeps the rest real — this file registers every core command at import).

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SlashContext } from "../ext/slashRegistry";
import { findSlashCommand } from "../ext/slashRegistry";
import { request } from "../lib/api/http";
import "./coreSlashCommands";
import { usageNoteMarkdown, windowLine } from "./usageView";

vi.mock("../lib/api/http", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api/http")>();
  return { ...mod, request: vi.fn() };
});

const mocked = vi.mocked(request);
const NOW = Date.parse("2026-10-23T18:52:00Z");

const snapshot = {
  accounts: [
    {
      provider_id: "anthropic-oauth",
      status: "ok" as const,
      windows: [
        { id: "session:0", kind: "session", used_pct: 1, resets_at: "2026-10-23T21:30:00Z", status: "ok" as const },
        { id: "weekly_all:1", kind: "weekly", used_pct: 85, resets_at: "2026-10-29T10:00:00Z", status: "warning" as const },
        { id: "weekly_scoped:2", kind: "weekly", used_pct: 0, resets_at: "2026-10-29T10:00:00Z", status: "ok" as const,
          scope: { model: "Fable" } },
      ],
    },
    { provider_id: "openai-codex", status: "unknown" as const },
  ],
};

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: "s1", noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

beforeEach(() => mocked.mockReset());

describe("/usage", () => {
  it("says used, never available, and keeps the unavailable account", () => {
    const md = usageNoteMarkdown(snapshot, NOW);
    expect(md).toContain("**Claude subscription**");
    expect(md).toContain("- Session — 1% used, resets in 2h 38m");
    expect(md).toContain("- Weekly — 85% used, resets in 5d 15h 8m (near limit)");
    expect(md).toContain("- Weekly · Fable — 0% used");
    expect(md).toContain("**ChatGPT / Codex subscription**\n- Usage unavailable");
    expect(md).not.toContain("vailable —");
  });

  it("marks a blocked window", () => {
    const w = { id: "x", kind: "session", used_pct: 100, resets_at: "2026-10-23T19:00:00Z", status: "blocked" as const };
    expect(windowLine(w, NOW)).toBe("Session — 100% used, resets in 8m (limit reached)");
  });

  it("posts the note from GET /api/usage", async () => {
    mocked.mockResolvedValue(snapshot);
    const noted: string[] = [];
    expect(findSlashCommand("usage")!.run(ctx({ noteToThread: (md) => noted.push(md) }))).toBe(true);
    await vi.waitFor(() => expect(noted).toHaveLength(1));
    expect(mocked).toHaveBeenCalledWith("/api/usage");
    expect(noted[0]).toContain("Subscription usage");
  });

  it("falls through without a session", () => {
    expect(findSlashCommand("usage")!.run(ctx({ sessionId: null }))).toBe(false);
    expect(mocked).not.toHaveBeenCalled();
  });
});
