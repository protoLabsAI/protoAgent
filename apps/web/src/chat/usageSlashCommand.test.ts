// /usage — chat-native subscription usage. Mocks only the http `request` (importOriginal
// keeps the rest real — this file registers every core command at import).

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { registeredChatComponents } from "../ext/componentRegistry";
import { chatStore } from "./chat-store";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SlashContext } from "../ext/slashRegistry";
import { findSlashCommand } from "../ext/slashRegistry";
import { request } from "../lib/api/http";
import "./coreSlashCommands";
import { clockTime, usageNoteMarkdown, windowLine } from "./usageView";

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
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

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

  it("appends persistent cards with independent check times and a text fallback", async () => {
    mocked.mockResolvedValue(snapshot);
    const sessionId = chatStore.createSession().id;
    const noteToThread = vi.fn();
    const clock = vi.spyOn(Date, "now").mockReturnValue(NOW);
    for (const now of [NOW, NOW + 1000]) {
      clock.mockReturnValue(now);
      expect(findSlashCommand("usage")!.run(ctx({ sessionId, noteToThread }))).toBe(true);
      await vi.waitFor(() => {
        const messages = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!.messages;
        expect(messages[messages.length - 1]).toMatchObject({
          role: "system", status: "done", createdAt: now,
          content: usageNoteMarkdown(snapshot, now),
          component: { component: "subscription-usage", props: { snapshot, checked_at: now } },
        });
        expect(messages[messages.length - 1]?.ephemeral).not.toBe(true);
      });
    }
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!.messages).toHaveLength(2);
    expect(mocked).toHaveBeenCalledWith("/api/usage");
    expect(noteToThread).not.toHaveBeenCalled();
    expect(usageNoteMarkdown(snapshot, NOW)).toMatch(new RegExp("^\\*\\*Subscription usage\\*\\* · checked " + clockTime(NOW)));
  });

  it("keeps request errors as danger notes", async () => {
    mocked.mockResolvedValue({});
    mocked.mockRejectedValueOnce(new Error("offline"));
    const noteToThread = vi.fn();
    findSlashCommand("usage")!.run(ctx({ noteToThread }));
    await vi.waitFor(() => expect(noteToThread).toHaveBeenCalledWith("Usage check failed — offline", { tone: "danger" }));
  });

  it("falls through without a session", () => {
    expect(findSlashCommand("usage")!.run(ctx({ sessionId: null }))).toBe(false);
    expect(mocked).not.toHaveBeenCalled();
  });
});

function renderCard(value: unknown, checked_at = NOW) {
  return renderToStaticMarkup(createElement(registeredChatComponents()["subscription-usage"], {
    props: { snapshot: value, checked_at },
  }));
}

describe("subscription usage card", () => {
  it("uses local zero-padded 24-hour check time", () => {
    expect(clockTime(new Date(2026, 9, 23, 7, 4, 2).getTime())).toBe("07:04:02");
    expect(renderCard(snapshot)).toContain("Checked " + clockTime(NOW));
    expect(renderCard(snapshot)).toContain(new Date(NOW).toLocaleString());
  });

  it("renders meters, states, cache age and stale status frozen at the check time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const data = { accounts: [{
      ...snapshot.accounts[0], stale: true, fetched_at: new Date(NOW - 120_000).toISOString(),
      windows: [
        { ...snapshot.accounts[0].windows![0], used_pct: 5, resets_at: new Date(NOW + 133 * 60_000).toISOString() },
        snapshot.accounts[0].windows![1],
        { ...snapshot.accounts[0].windows![0], used_pct: 100, status: "blocked" },
      ],
    }] };
    const html = renderCard(data);
    expect((html.match(/role="progressbar"/g) ?? [])).toHaveLength(3);
    for (const text of ["Session · 5% used", "Resets in 2h 13m", "Near limit", "Limit reached",
      "data from 2m ago", "last known, refresh failed"]) expect(html).toContain(text);
    vi.advanceTimersByTime(3600_000);
    expect(renderCard(data)).toBe(html);
    expect(renderCard({ accounts: [{ ...data.accounts[0], fetched_at: new Date(NOW - 10_000).toISOString() }] })).not.toContain("data from");
  });

  it("handles unavailable, empty and malformed data", () => {
    expect(renderCard(snapshot)).toContain("Usage unavailable");
    expect(renderCard({ accounts: [{ provider_id: "anthropic-oauth", status: "ok", windows: [] }] })).toContain("Usage unavailable");
    expect(renderCard({ accounts: [] })).toContain("No Claude or ChatGPT / Codex subscription is connected.");
    for (const bad of [null, undefined, {}, { accounts: {} }, { accounts: [null] },
      { accounts: [{ ...snapshot.accounts[0], windows: [null] }] },
      { accounts: [{ ...snapshot.accounts[0], windows: [{ kind: {} }] }] }]) {
      expect(renderCard(bad)).toContain("Usage unavailable");
    }
    expect(renderCard({ accounts: [{ provider_id: "__proto__", status: "unknown" }] })).toContain("Usage unavailable");
    expect(renderCard(snapshot, NaN)).toContain("Usage unavailable");
  });
});
