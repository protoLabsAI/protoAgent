import { describe, expect, it } from "vitest";

import { sessionCast } from "./chat-store";
import type { ChatMessage } from "../lib/types";

// Who is in this chat (#3049) — DERIVED from the transcript. A tracked list can only
// drift (the first cut grew chips a deleted draft left behind); the transcript can't.

const msg = (over: Partial<ChatMessage>): ChatMessage => ({
  role: "assistant",
  content: "hi",
  ...over,
});

describe("sessionCast", () => {
  it("is who has SPOKEN, in first-spoken order", () => {
    const messages = [
      msg({ role: "user", content: "@proto go" }),
      msg({ author: { name: "proto" }, content: "done" }),
      msg({ author: { name: "reviewer" }, content: "confirmed" }),
      msg({ author: { name: "proto" }, content: "again" }),
    ];
    expect(sessionCast({ messages })).toEqual(["proto", "reviewer"]);
  });

  it("an ordinary chat has no cast", () => {
    const messages = [msg({ role: "user", content: "hello" }), msg({ content: "hi there" })];
    expect(sessionCast({ messages })).toEqual([]);
    expect(sessionCast(undefined)).toEqual([]);
  });

  it("the lead's own unattributed messages are not participants", () => {
    // The lead is always present — listing it is noise, and its messages carry no author.
    expect(sessionCast({ messages: [msg({ content: "I checked the build" })] })).toEqual([]);
  });

  it("typing an @ that never sends adds nobody", () => {
    // The regression that killed the tracked list: a chip from a deleted draft. Derived
    // cast has no keystroke path at all — only a message in the transcript counts.
    expect(sessionCast({ messages: [] })).toEqual([]);
  });

  it("a user system note with no author adds nobody", () => {
    expect(sessionCast({ messages: [msg({ role: "system", content: "note" })] })).toEqual([]);
  });
});
