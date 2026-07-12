import { describe, expect, it } from "vitest";

import { parseMultimodalEnvelope } from "./multimodalEnvelope";

// The wire shape from graph/multimodal.py::multimodal_tool_result — the \x1e record
// separator + tag, then json.dumps({"text": …, "images": […]}) with text FIRST.
const SENTINEL = "\x1e[multimodal-tool-v1]";
const envelope = (text: string, images: unknown[] = [{ b64: "aGk=", mime: "image/png" }]) =>
  SENTINEL + JSON.stringify({ text, images });

describe("parseMultimodalEnvelope (#1947)", () => {
  it("returns null for ordinary tool outputs (plain text / JSON / errors untouched)", () => {
    expect(parseMultimodalEnvelope("42 = 42")).toBeNull();
    expect(parseMultimodalEnvelope('{"ok": true}')).toBeNull();
    expect(parseMultimodalEnvelope("Error: boom")).toBeNull();
    expect(parseMultimodalEnvelope("")).toBeNull();
    // The tag mid-string is NOT an envelope — only a prefix counts.
    expect(parseMultimodalEnvelope("see [multimodal-tool-v1] docs")).toBeNull();
  });

  it("parses a complete envelope: text + image count, never the base64", () => {
    const env = parseMultimodalEnvelope(envelope("chart rendered", [{ b64: "AA==" }, { b64: "BB==" }]));
    expect(env).toEqual({ text: "chart rendered", imageCount: 2, truncated: false });
  });

  it("tolerates a transport that stripped the \\x1e control char", () => {
    const bare = envelope("hi").slice(1); // "[multimodal-tool-v1]{…}"
    expect(parseMultimodalEnvelope(bare)).toEqual({ text: "hi", imageCount: 1, truncated: false });
  });

  it("unescapes the text through JSON (quotes, newlines)", () => {
    const env = parseMultimodalEnvelope(envelope('a "quoted"\nline'));
    expect(env?.text).toBe('a "quoted"\nline');
  });

  it("recovers the text from an envelope the 800-char server preview cut mid-base64", () => {
    // The robustness case: server/chat.py truncates tool previews to _TOOL_PREVIEW_CHARS=800,
    // and the images' base64 is megabytes — so the console routinely receives an envelope
    // whose JSON does not parse. The text field (first key, short caption) survives.
    const full = envelope("chart rendered", [{ b64: "Q".repeat(5000), mime: "image/png" }]);
    const env = parseMultimodalEnvelope(full.slice(0, 800));
    expect(env).toEqual({ text: "chart rendered", imageCount: null, truncated: true });
  });

  it("degrades to a generic (empty-text) result when even the text was cut", () => {
    const cut = (SENTINEL + '{"text": "a very long caption that the preview chops mid-').slice(0, 60);
    const env = parseMultimodalEnvelope(cut);
    expect(env).toEqual({ text: "", imageCount: null, truncated: true });
  });

  it("treats a malformed images field as zero images (text still renders)", () => {
    const env = parseMultimodalEnvelope(SENTINEL + JSON.stringify({ text: "hi", images: "nope" }));
    expect(env).toEqual({ text: "hi", imageCount: 0, truncated: false });
  });
});
