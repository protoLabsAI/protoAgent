import { beforeEach, describe, expect, it } from "vitest";

import { chatStore, unusedSession, DEFAULT_SESSION_TITLE } from "./chat-store";
import type { ChatState } from "./chat-store";

function blank(over: Partial<ChatState["sessions"][number]> = {}) {
  return {
    id: "s1",
    title: DEFAULT_SESSION_TITLE,
    messages: [],
    createdAt: 1,
    updatedAt: 1,
    ...over,
  };
}

describe("unusedSession with participants (#3049)", () => {
  const state = (sessions: ChatState["sessions"]) =>
    ({ sessions, currentSessionId: null, activeSessions: [], sessionStatusMap: {}, version: 1,
       pendingDeleteRequest: null }) as unknown as ChatState;

  it("does not hand back a plain blank when a room was asked for", () => {
    // Reusing it would silently lose the roster the operator just chose.
    expect(unusedSession(state([blank()]), { participants: ["proto"] })).toBeUndefined();
  });

  it("does not hand back a room when a plain chat was asked for", () => {
    // The reverse surprise: asking for a new chat and landing in a room.
    expect(unusedSession(state([blank({ participants: ["proto"] })]), {})).toBeUndefined();
  });

  it("reuses a room with the same membership regardless of order", () => {
    const s = blank({ participants: ["proto", "reviewer"] });
    expect(unusedSession(state([s]), { participants: ["reviewer", "proto"] })).toBe(s);
  });

  it("still reuses a plain blank for a plain request", () => {
    const s = blank();
    expect(unusedSession(state([s]), {})).toBe(s);
  });
});

describe("participant mutators", () => {
  let id: string;

  beforeEach(() => {
    id = chatStore.createSession({ participants: ["proto"] }).id;
  });

  it("creates a session carrying its roster", () => {
    const s = chatStore.getSnapshot().sessions.find((x) => x.id === id);
    expect(s?.participants).toEqual(["proto"]);
  });

  it("adds a participant idempotently, preserving order", () => {
    chatStore.addSessionParticipant(id, "reviewer");
    chatStore.addSessionParticipant(id, "proto");
    const s = chatStore.getSnapshot().sessions.find((x) => x.id === id);
    expect(s?.participants).toEqual(["proto", "reviewer"]);
  });

  it("ignores an empty name rather than adding a blank chip", () => {
    chatStore.addSessionParticipant(id, "");
    expect(chatStore.getSnapshot().sessions.find((x) => x.id === id)?.participants).toEqual(["proto"]);
  });

  it("de-duplicates on replace while keeping the operator's order", () => {
    chatStore.setSessionParticipants(id, ["reviewer", "proto", "reviewer"]);
    expect(chatStore.getSnapshot().sessions.find((x) => x.id === id)?.participants).toEqual([
      "reviewer",
      "proto",
    ]);
  });

  it("emptying the roster turns the tab back into an ordinary chat", () => {
    // `undefined`, not `[]` — an empty array would render an empty roster strip and
    // make every "is this a room?" check subtly wrong.
    chatStore.setSessionParticipants(id, []);
    expect(chatStore.getSnapshot().sessions.find((x) => x.id === id)?.participants).toBeUndefined();
  });
});

describe("roster ordering for the @ popover (#3049)", () => {
  // The ordering rule the composer applies, extracted so it's testable without
  // mounting the surface: room members first in the operator's order, everyone else
  // after, and NOBODY filtered out — refusing to route `@somebody-else` would be a
  // surprise, not a safety property.
  const order = (all: string[], roster: string[]) => {
    if (!roster.length) return all;
    const rank = (n: string) => (roster.indexOf(n) === -1 ? roster.length : roster.indexOf(n));
    return [...all].sort((a, b) => rank(a) - rank(b));
  };

  it("puts room members first, in the order the room was built", () => {
    expect(order(["alice", "proto", "bob", "reviewer"], ["reviewer", "proto"])).toEqual([
      "reviewer",
      "proto",
      "alice",
      "bob",
    ]);
  });

  it("never drops a non-member — every delegate stays addressable", () => {
    const all = ["alice", "proto", "bob"];
    expect(order(all, ["proto"]).sort()).toEqual(all.sort());
  });

  it("leaves an ordinary chat's list untouched", () => {
    expect(order(["alice", "proto"], [])).toEqual(["alice", "proto"]);
  });
});
