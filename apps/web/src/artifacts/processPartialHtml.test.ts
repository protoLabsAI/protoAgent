import { describe, expect, it } from "vitest";

import {
  bodyBytesWithoutStyle,
  extractCompleteStyles,
  firstStyleClosed,
  PREVIEW_BODY_BYTE_THRESHOLD,
  processPartialHtml,
} from "./processPartialHtml";

// ADR 0118 (D3 console preview): sanitiser for HTML that is still streaming in.
// Ported from OIU (MIT); see processPartialHtml.ts header for attribution.

describe("processPartialHtml", () => {
  it("drops the incomplete tag at the end of the stream", () => {
    expect(processPartialHtml('<p>hi</p><div class="x"')).toBe("<p>hi</p>");
    expect(processPartialHtml("<p>done</p><spa")).toBe("<p>done</p>");
  });

  it("drops a half-streamed HTML entity at the end", () => {
    expect(processPartialHtml("<p>5 &lt; 6 &am")).toBe("<p>5 &lt; 6 ");
  });

  it("strips complete <script> blocks", () => {
    expect(processPartialHtml("<p>a</p><script>alert(1)</script><p>b</p>")).toBe("<p>a</p><p>b</p>");
  });

  it("strips an incomplete (unclosed) <script> block", () => {
    expect(processPartialHtml("<div>ok</div><script>bad(")).toBe("<div>ok</div>");
  });

  it("strips <head> and renders body content", () => {
    expect(processPartialHtml("<head><title>t</title></head><body><p>hi</p></body>")).toBe("<p>hi</p>");
  });

  it("strips an incomplete <style> block", () => {
    expect(processPartialHtml("<p>a</p><style>.x{color:red")).toBe("<p>a</p>");
  });

  it("hoists a complete <style> block to the top", () => {
    const out = processPartialHtml("<body><p>hi</p><style>.x{color:red}</style></body>");
    expect(out).toBe("<style>.x{color:red}</style><p>hi</p>");
  });

  it("hoists a <style> out of <head>, keeping the body below it", () => {
    const out = processPartialHtml("<head><style>.a{top:0}</style></head><body><p>x</p></body>");
    expect(out).toBe("<style>.a{top:0}</style><p>x</p>");
  });

  it("strips inline on*= event-handler attributes (quoted and bare), keeping other attrs", () => {
    const out = processPartialHtml(
      `<img src="x" onerror="steal()" /><button onclick='go()'>b</button><div onmouseover=hack()>hi</div>`,
    );
    expect(out).toBe('<img src="x" /><button>b</button><div>hi</div>');
    expect(out).not.toMatch(/on\w+=/i);
  });

  it("leaves non-handler attributes that merely start with 'on-ish' names alone", () => {
    // `data-on` is not an event handler — a bare `on` must follow whitespace to match.
    expect(processPartialHtml('<div data-once="1">keep</div>')).toBe('<div data-once="1">keep</div>');
  });

  it("handles a realistic partial document end to end", () => {
    const out = processPartialHtml(
      '<!DOCTYPE html><html><head><style>.c{color:blue}</style></head>' +
        '<body><h1>Title</h1><script>track()</script><p onclick="x()">para</p><div incomplet',
    );
    expect(out).toBe("<style>.c{color:blue}</style><h1>Title</h1><p>para</p>");
    expect(out).not.toMatch(/script|onclick|<head|DOCTYPE|incomplet/i);
  });

  it("returns the full string unchanged when there is nothing to sanitise", () => {
    expect(processPartialHtml("<p>plain</p>")).toBe("<p>plain</p>");
  });
});

describe("extractCompleteStyles", () => {
  it("concatenates every complete style block and ignores an unclosed one", () => {
    expect(extractCompleteStyles("<style>.a{}</style>mid<style>.b{}</style><style>.c{")).toBe(
      "<style>.a{}</style><style>.b{}</style>",
    );
    expect(extractCompleteStyles("<p>no styles</p>")).toBe("");
  });
});

describe("gating helpers", () => {
  const shouldShowPreview = (html: string): boolean =>
    firstStyleClosed(html) || bodyBytesWithoutStyle(html) >= PREVIEW_BODY_BYTE_THRESHOLD;

  describe("firstStyleClosed", () => {
    it("is true only once the first <style> block has closed", () => {
      expect(firstStyleClosed("<style>.x{}</style><p>hi</p>")).toBe(true);
      expect(firstStyleClosed("<p>hi</p><style>.x{color:")).toBe(false);
      expect(firstStyleClosed("<p>hi</p>")).toBe(false);
    });
  });

  describe("bodyBytesWithoutStyle", () => {
    it("reports zero while any <style> (open or closed) is present", () => {
      expect(bodyBytesWithoutStyle("<style>.x{}</style><p>hi</p>")).toBe(0);
      expect(bodyBytesWithoutStyle("<p>hi</p><style>.x{color:")).toBe(0);
    });

    it("counts body markup bytes when no style has appeared", () => {
      expect(bodyBytesWithoutStyle("<p>abc</p>")).toBe("<p>abc</p>".length);
    });

    it("counts only <body> content when a body tag is present", () => {
      const body = "a".repeat(2000);
      expect(bodyBytesWithoutStyle(`<html><head></head><body>${body}</body></html>`)).toBe(2000);
    });

    it("counts UTF-8 bytes, not code units, for multi-byte characters", () => {
      expect(bodyBytesWithoutStyle("é")).toBe(2); // U+00E9 → 2 bytes
      expect(bodyBytesWithoutStyle("😀")).toBe(4); // surrogate pair → 4 bytes
    });
  });

  describe("the 1.5 KB threshold", () => {
    it("holds a style-first stream until the style closes, then shows", () => {
      expect(shouldShowPreview("<style>.x{color:")).toBe(false); // style open, body held
      expect(shouldShowPreview("<style>.x{color:red}</style><p>hi</p>")).toBe(true);
    });

    it("holds a style-less stream below 1.5 KB and shows at the threshold", () => {
      expect(PREVIEW_BODY_BYTE_THRESHOLD).toBe(1536);
      expect(shouldShowPreview("a".repeat(PREVIEW_BODY_BYTE_THRESHOLD - 1))).toBe(false);
      expect(shouldShowPreview("a".repeat(PREVIEW_BODY_BYTE_THRESHOLD))).toBe(true);
    });
  });
});
