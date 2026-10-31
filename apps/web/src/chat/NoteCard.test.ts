import { describe, expect, it } from "vitest";

import { hasNoteLede, noteCardParts } from "./NoteCard";

describe("noteCardParts", () => {
  it("an explicit title wins and leaves the body whole", () => {
    expect(noteCardParts({ content: "**Goal set.** ship it", noteTitle: "Goal", noteTone: "success" })).toEqual({
      title: "Goal",
      body: "**Goal set.** ship it",
    });
  });

  it("a leading bold lede becomes the title and leaves the body", () => {
    expect(noteCardParts({ content: "**Goal set.** until the tests pass", noteTone: "success" })).toEqual({
      title: "Goal set",
      body: "until the tests pass",
    });
    expect(noteCardParts({ content: "**↪ Aside:** what did we decide?" }).title).toBe("↪ Aside");
    expect(noteCardParts({ content: "**Watch set.**" })).toEqual({ title: "Watch set", body: "" });
  });

  it("bold text that isn't a lede stays in the body and the tone names the card", () => {
    expect(noteCardParts({ content: "Reasoning effort set to **high**.", noteTone: "info" })).toEqual({
      title: "Note",
      body: "Reasoning effort set to **high**.",
    });
    expect(noteCardParts({ content: "boom", noteTone: "danger" }).title).toBe("Something went wrong");
    expect(noteCardParts({ content: "x" }).title).toBe("Note");
  });

  it("hasNoteLede matches only a leading bold lede", () => {
    expect(hasNoteLede("**Goal set.** x")).toBe(true);
    expect(hasNoteLede("Model set to **opus**")).toBe(false);
  });
});
