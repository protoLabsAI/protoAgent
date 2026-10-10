// Ported from OpenIntelligentUI (OIU), MIT-licensed.
//   repo:   https://github.com/CopilotKit/OpenIntelligentUI
//   path:   apps/app/src/components/generative-ui/open-generative-ui/process-partial-html.ts
//   commit: f6e4388
//
// The MIT License — Copyright (c) Atai Barkai
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND. See the OIU
// LICENSE for the full text.
//
// ADR 0118 (D3 console preview): adapted from OIU's processPartialHtml for
// streamed artifact previews. Changes from upstream: complete <style> blocks are
// hoisted to the top rather than dropped, inline `on*=` event-handler attributes
// are stripped defensively, and gating helpers (firstStyleClosed /
// bodyBytesWithoutStyle) decide when a partial stream is safe to show.

/** 1.5 KB of body markup — once this much has streamed with no <style>, show the preview. */
export const PREVIEW_BODY_BYTE_THRESHOLD = 1536;

/**
 * Extracts every complete `<style>…</style>` block from the raw HTML, in order.
 * Returns the concatenated style tags, suitable for hoisting to the top of a preview.
 */
export function extractCompleteStyles(html: string): string {
  const matches = html.match(/<style\b[^>]*>[\s\S]*?<\/style>/gi);
  return matches ? matches.join("") : "";
}

/**
 * Removes inline `on*=` event-handler attributes (onclick, onerror, onload, …),
 * whether quoted or bare. Defensive only — scripts are already stripped — but it
 * keeps a half-streamed handler from ever reaching the preview DOM.
 */
function stripEventHandlers(html: string): string {
  return html.replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");
}

/**
 * Processes raw accumulated HTML for safe preview injection while it is still
 * streaming. Pure function — no DOM, no globals.
 *
 * Pipeline (order matters):
 * 1. Drop the incomplete tag at the end of the stream.
 * 2. Hoist complete `<style>` blocks out of the body (re-prepended at the end).
 * 3. Strip complete `<script>`/`<head>` blocks.
 * 4. Strip incomplete `<style>`/`<script>`/`<head>` blocks (the streaming tail).
 * 5. Drop a half-streamed HTML entity at the end.
 * 6. Extract `<body>` content when present (else keep the whole string).
 * 7. Strip inline `on*=` event-handler attributes.
 * 8. Prepend the hoisted styles.
 */
export function processPartialHtml(html: string): string {
  let result = html;

  // 1. Drop an incomplete tag at the very end (e.g. a dangling "<div cla").
  result = result.replace(/<[^>]*$/, "");

  // 2. Pull complete <style> blocks aside so we can hoist them, then remove them
  //    from their original position (they may sit inside <head>, stripped below).
  const hoistedStyles = extractCompleteStyles(result);
  result = result.replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, "");

  // 3. Strip complete <script>/<head> blocks entirely.
  result = result.replace(/<(script|head)\b[^>]*>[\s\S]*?<\/\1>/gi, "");

  // 4. Strip incomplete <style>/<script>/<head> blocks — the unclosed streaming tail.
  result = result.replace(/<(style|script|head)\b[^>]*>[\s\S]*$/gi, "");

  // 5. Drop a half-streamed HTML entity at the end (e.g. "&amp" with no ";").
  result = result.replace(/&[a-zA-Z0-9#]*$/, "");

  // 6. Extract body content when present; otherwise use the full remaining string.
  const bodyMatch = result.match(/<body[^>]*>([\s\S]*)/i);
  if (bodyMatch) {
    result = bodyMatch[1] ?? "";
    result = result.replace(/<\/body>[\s\S]*/i, "");
  }

  // 7. Strip inline event-handler attributes defensively.
  result = stripEventHandlers(result);

  // 8. Hoist the complete styles to the top so markup below them is styled.
  return hoistedStyles + result;
}

/**
 * True once the first `<style>` block has fully closed. A streamed preview holds
 * until this is true (so it doesn't flash unstyled) — unless the body grows past
 * {@link PREVIEW_BODY_BYTE_THRESHOLD} with no style in sight.
 */
export function firstStyleClosed(html: string): boolean {
  return /<style\b[^>]*>[\s\S]*?<\/style>/i.test(html);
}

/** UTF-8 byte length of a string, without pulling in TextEncoder or Buffer. */
function utf8ByteLength(str: string): number {
  let bytes = 0;
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff) {
      // High surrogate — a 4-byte code point paired with the following low surrogate.
      bytes += 4;
      i++;
    } else bytes += 3;
  }
  return bytes;
}

/**
 * Bytes of body markup accumulated so far while no `<style>` has appeared. Returns
 * 0 the moment any `<style>` (open or closed) is seen — then the preview waits on
 * {@link firstStyleClosed} instead, rather than racing a half-streamed stylesheet.
 */
export function bodyBytesWithoutStyle(html: string): number {
  if (/<style\b/i.test(html)) return 0;

  const bodyMatch = html.match(/<body[^>]*>([\s\S]*)/i);
  let body = bodyMatch ? (bodyMatch[1] ?? "") : html;
  body = body.replace(/<\/body>[\s\S]*/i, "");
  return utf8ByteLength(body);
}
