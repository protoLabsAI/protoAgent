import { beforeEach, describe, expect, it } from "vitest";

import { composerDraftContext, quoteEntry, replyContext, replyToHomeEntry } from "./homeReply";
import { loadDraft, loadDraftContext, saveDraft, saveDraftContext } from "./scratchState";

const HOME = "chat-home-reports";
const reply = (title: string) => ({ quote: quoteEntry(title, "Body line."), context: replyContext(`the Home entry "${title}"`) });

beforeEach(() => sessionStorage.clear());

describe("composerDraftContext — a Reply reference follows its quote", () => {
  it("drops the reference when the quote was deleted from the message", () => {
    const a = reply("Fleet member sol stopped");
    replyToHomeEntry(HOME, a);
    expect(loadDraftContext(HOME)).toBe(a.context);
    expect(composerDraftContext(HOME, "an unrelated question")).toBe("");
    // Dropped for good: the next message does not get it either.
    expect(loadDraftContext(HOME)).toBe("");
    expect(composerDraftContext(HOME, "another message")).toBe("");
  });

  it("keeps the reference while the quote's title line is in the message, even trimmed", () => {
    const a = reply("Fleet member sol stopped");
    replyToHomeEntry(HOME, a);
    expect(composerDraftContext(HOME, "> Fleet member sol stopped\n\nrestart it")).toBe(a.context);
    // Sent once, it is ordinary draft context: kept for a retry until the send succeeds.
    expect(composerDraftContext(HOME, "try again")).toBe(a.context);
  });

  it("drops only the deleted quote's reference of two replies", () => {
    const a = reply("Fleet member sol stopped");
    const b = reply("Spend cap passed");
    replyToHomeEntry(HOME, a);
    replyToHomeEntry(HOME, b);
    replyToHomeEntry(HOME, b); // a second click does not name the entry twice
    expect(loadDraftContext(HOME)).toBe(`${a.context}\n${b.context}`);
    expect(composerDraftContext(HOME, `${b.quote}raise it`)).toBe(b.context);
  });

  it("leaves context that is not a Reply reference alone", () => {
    saveDraftContext(HOME, "<!-- Continuing session \"ctx\". -->");
    expect(composerDraftContext(HOME, "hello")).toBe("<!-- Continuing session \"ctx\". -->");
    // A reply on an empty draft keeps what was already waiting (e.g. kept for a retry).
    const a = reply("Fleet member sol stopped");
    replyToHomeEntry(HOME, a);
    expect(loadDraft(HOME)).toBe(a.quote);
    expect(loadDraftContext(HOME)).toBe(`<!-- Continuing session "ctx". -->\n${a.context}`);
    saveDraft(HOME, "");
    expect(composerDraftContext(HOME, "hello again")).toBe("<!-- Continuing session \"ctx\". -->");
  });
});
