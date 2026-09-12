import { describe, expect, it } from "vitest";

import { CHAT_ATTACH_ACCEPT, DOCX_MIME, KNOWLEDGE_INGEST_ACCEPT } from "./attachTypes";

const tokens = (accept: string) => accept.split(",");

describe("file picker accept lists — the formats the ingestion engine extracts", () => {
  it.each([
    ["chat composer", CHAT_ATTACH_ACCEPT],
    ["knowledge ingest", KNOWLEDGE_INGEST_ACCEPT],
  ])("%s offers Word .docx by extension and MIME, never legacy .doc", (_surface, accept) => {
    const t = tokens(accept);
    expect(t).toContain(".docx");
    expect(t).toContain(DOCX_MIME);
    expect(t).not.toContain(".doc"); // the server refuses it (415) — don't offer it
    expect(t).toEqual(expect.arrayContaining([".txt", ".md", ".html", ".pdf", ".mp3", ".mp4"]));
  });

  it("only the chat composer takes images (they ride the turn natively)", () => {
    expect(tokens(CHAT_ATTACH_ACCEPT)).toContain(".png");
    expect(tokens(KNOWLEDGE_INGEST_ACCEPT)).not.toContain(".png");
  });

  it("is a well-formed accept attribute: no blanks, no duplicates", () => {
    for (const accept of [CHAT_ATTACH_ACCEPT, KNOWLEDGE_INGEST_ACCEPT]) {
      const t = tokens(accept);
      expect(t.every((x) => x.length > 0 && x === x.trim())).toBe(true);
      expect(new Set(t).size).toBe(t.length);
    }
  });
});
