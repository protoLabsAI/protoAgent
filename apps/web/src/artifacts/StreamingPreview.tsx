// The throttle/flush cadence (1 update/s with an immediate flush on milestones) and the
// reported-height clamp below are ported from OpenIntelligentUI (OIU), MIT-licensed.
//   repo:   https://github.com/CopilotKit/OpenIntelligentUI
//   path:   apps/app/src/components/generative-ui/open-generative-ui/renderer.tsx
//   commit: f6e4388
//
// The MIT License — Copyright (c) Atai Barkai
//
// Permission is hereby granted, free of charge, to any person obtaining a copy of this
// software and associated documentation files (the "Software"), to deal in the Software
// without restriction, including without limitation the rights to use, copy, modify, merge,
// publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
// to whom the Software is furnished to do so, subject to the following conditions: the above
// copyright notice and this permission notice shall be included in all copies or substantial
// portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
// See the OIU LICENSE for the full text.

import { Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { ToolArgsBuffer } from "../chat/toolArgsBuffer";
import { refName } from "./artifactRef";
import { clampHeight, MIN_FRAME_HEIGHT } from "./inlineFrames";
import {
  bodyBytesWithoutStyle,
  firstStyleClosed,
  PREVIEW_BODY_BYTE_THRESHOLD,
  processPartialHtml,
} from "./processPartialHtml";
// Byte-for-byte vendored Idiomorph (0BSD) — see vendor/idiomorph.LICENSE.txt for version + sha256.
// Imported as raw text so it can be inlined as the frame's one nonce'd script.
import idiomorphMin from "./vendor/idiomorph.min.js?raw";

// Streamed artifact preview (ADR 0118 D3 console, S8b). While a `show_artifact` tool call with
// inline placement is still streaming, the server decodes its declared `code` argument into a
// live per-tool-call buffer (S3, chat/toolArgsBuffer.ts); this component renders the inline card
// AT ONCE from that buffer, rather than leaving the operator on a spinner until the tool ends.
//
//   • The card header (title + kind-aware status line) shows immediately. The title appears as
//     soon as the caller knows it; before then a kind-derived name stands in.
//   • For `html`/`svg` ONLY, once the gate opens the card hosts a PREVIEW FRAME. Every other kind
//     (`react`, `mermaid`, `vega-lite`, …) shows the placeholder only — a partial program or spec
//     has nothing safe to preview.
//
// Trust model — the preview frame is a tighter fence than the inline artifact host (S7b), because
// it renders half-streamed, model-written markup:
//   • `sandbox="allow-scripts"` with NO `allow-same-origin`, so the frame runs on a unique opaque
//     origin with no access to the console's cookies/localStorage (where the operator bearer lives)
//     and no first-party API reach. No bearer is ever posted to it.
//   • Its CSP meta is `default-src 'none'; script-src 'nonce-<n>'; style-src 'unsafe-inline';
//     img-src data: blob:`, with a FRESH random nonce per frame. The only script carrying the nonce
//     is the host's own morph script (vendored Idiomorph + a tiny postMessage receiver); any script
//     that rides in on the streamed markup has no nonce and so cannot execute, and inline `on*=`
//     handlers are inert because there is no `'unsafe-inline'` for scripts.
//   • All markup is run through S8a's `processPartialHtml` BEFORE it is posted: the incomplete
//     trailing tag is dropped, `<script>`/`<head>`/incomplete `<style>` blocks are stripped,
//     complete `<style>` blocks are hoisted, and inline handlers are removed. So model code never
//     reaches the frame in the first place — the CSP is the second, independent fence.
//   • The frame is opaque-origin, so a host→frame post cannot name a concrete target origin; we
//     post with `"*"`, which is safe precisely because the payload is non-secret model markup.
//
// Gating (OIU's "never unstyled" rule): the frame stays hidden until the first `<style>` block
// closes, or until 1.5 KB of body markup has streamed with no style in sight — so the preview does
// not flash unstyled, nor stall forever on a style-less document.
//
// Cadence (S8c, OIU renderer.tsx): markup is posted at most ONCE A SECOND so a fast stream does
// not thrash the frame, with an IMMEDIATE flush on the milestones where a stale frame reads worst —
// the first `<style>` closing (the first styled paint), the first body markup appearing, and the
// stream's `done`. The frame reports its content height back (`proto-preview:height`); we clamp it
// to the inline-frame bounds and size the preview from it.
//
// Handover (S8c): when the stream is `done` and the caller has the real artifact-ref in hand, it
// passes `renderFinal`; this component then renders that final inline frame (S7b) IN PLACE of the
// preview, seeded with the last measured height so the transcript layout does not jump as the
// sandboxed preview gives way to the artifact's own embed.

/** The kinds that get a live preview frame; every other kind shows the placeholder only. */
const PREVIEW_KINDS = new Set(["html", "svg"]);

/** The preview frame's root node — the morph receiver targets it; the host builds it empty. */
const PREVIEW_ROOT_ID = "proto-preview-root";

/** The postMessage type the host sends and the in-frame receiver listens for. */
const PREVIEW_MORPH_TYPE = "proto-preview:morph";

/** The postMessage type the FRAME sends back after each morph, carrying its content height. */
const PREVIEW_HEIGHT_TYPE = "proto-preview:height";

/** The preview frame's starting height, used until the frame reports its own content height
 *  (then clamped to the inline-frame bounds). Also the height handed to the final inline frame if
 *  the preview never measured — so the handover never collapses the slot to nothing. */
const PREVIEW_FRAME_HEIGHT = 240;

/** At most one markup post per second (OIU renderer.tsx), save for the milestone flushes below. */
const PREVIEW_THROTTLE_MS = 1000;

const STATUS_BY_KIND: Record<string, string> = {
  html: "Preparing preview…",
  svg: "Preparing preview…",
  react: "Writing React component…",
  mermaid: "Rendering diagram…",
  "vega-lite": "Rendering chart…",
  markdown: "Writing document…",
  file: "Preparing file…",
};

/** The card's status line: kind-aware while streaming, and a short "finishing" note once the final
 *  frame for the arg has arrived (the real inline frame takes over in S8c). */
function statusLine(kind: string, done: boolean): string {
  if (done) return "Finishing…";
  return STATUS_BY_KIND[kind] ?? "Writing artifact…";
}

/** The exact CSP the preview frame carries. Single source of truth so the srcdoc and any guard
 *  read the same string. */
export function previewCsp(nonce: string): string {
  return `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; img-src data: blob:`;
}

/** OIU's gate: show the preview once the first `<style>` has closed (so it is never unstyled), or
 *  once {@link PREVIEW_BODY_BYTE_THRESHOLD} bytes of body markup have arrived with no style at all.
 *
 *  This is a POINT-IN-TIME predicate on the current buffer, NOT monotonic on its own: once the byte
 *  branch has opened the gate, a later still-opening `<style>` makes `bodyBytesWithoutStyle` read 0
 *  again while `firstStyleClosed` is still false, so this flips back to false mid-stream. The
 *  component is what makes "shown" monotonic — {@link StreamingPreview} latches this open so the
 *  frame never unmounts and remounts underneath an in-flight post. */
export function previewGateOpen(html: string): boolean {
  return firstStyleClosed(html) || bodyBytesWithoutStyle(html) >= PREVIEW_BODY_BYTE_THRESHOLD;
}

/** A fresh random nonce per frame — hex of 16 random bytes. Falls back to `Math.random` only where
 *  the Web Crypto API is unavailable (it is present in every supported browser and in jsdom). */
function randomNonce(): string {
  const bytes = new Uint8Array(16);
  const webCrypto = typeof crypto !== "undefined" ? crypto : undefined;
  if (webCrypto && typeof webCrypto.getRandomValues === "function") {
    webCrypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

/** The in-frame receiver: on each `proto-preview:morph` post, morph the root's children to match
 *  the processed markup so the preview updates in place instead of being rebuilt, then post the
 *  document's content height back so the host can size the frame (and hand that height to the
 *  final inline frame on swap). Built from the module constants so the host and the frame agree on
 *  the message types and the root id. */
const MORPH_RECEIVER = [
  "(function(){",
  "function report(){",
  "try{",
  "var h=Math.ceil(document.documentElement.scrollHeight||document.body.scrollHeight||0);",
  `parent.postMessage({type:${JSON.stringify(PREVIEW_HEIGHT_TYPE)},height:h},"*");`,
  "}catch(_){}",
  "}",
  "function render(html){",
  `var root=document.getElementById(${JSON.stringify(PREVIEW_ROOT_ID)});`,
  "if(!root)return;",
  'try{Idiomorph.morph(root,html,{morphStyle:"innerHTML"});}catch(_){root.innerHTML="";}',
  "report();",
  "}",
  'window.addEventListener("message",function(ev){',
  "var d=ev.data;",
  `if(!d||d.type!==${JSON.stringify(PREVIEW_MORPH_TYPE)}||typeof d.html!=="string")return;`,
  "render(d.html);",
  "});",
  "})();",
].join("");

/** Build the preview frame's srcdoc: the exact CSP meta, a minimal reset style (allowed by
 *  `style-src 'unsafe-inline'`), the empty morph root, and ONE nonce'd script carrying the vendored
 *  Idiomorph plus the receiver. Any `</script` in the vendored lib is defused so it cannot close
 *  its host `<script>` early (there are none at the pinned version — belt-and-braces). */
function buildPreviewSrcdoc(nonce: string): string {
  const idiomorph = idiomorphMin.replace(/<\/(script)/gi, "<\\/$1");
  return [
    "<!doctype html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    `<meta http-equiv="Content-Security-Policy" content="${previewCsp(nonce)}">`,
    "<style>html,body{margin:0;padding:8px;font:14px/1.5 system-ui,-apple-system,sans-serif;color-scheme:light dark;}</style>",
    "</head>",
    "<body>",
    `<div id="${PREVIEW_ROOT_ID}"></div>`,
    `<script nonce="${nonce}">${idiomorph}\n${MORPH_RECEIVER}</script>`,
    "</body>",
    "</html>",
  ].join("\n");
}

/** Post the processed markup to the (opaque-origin) preview frame. Target `"*"` is required — an
 *  opaque origin never matches a concrete one — and safe because the payload is non-secret model
 *  markup already stripped of scripts/handlers. Swallows the detached/cross-origin throw. */
function postMarkup(win: Window | null | undefined, html: string): void {
  if (!win) return;
  try {
    win.postMessage({ type: PREVIEW_MORPH_TYPE, html }, "*");
  } catch {
    /* detached / cross-origin — best effort */
  }
}

export type StreamingPreviewProps = {
  /** The streamed `code` argument so far, plus whether its final frame has arrived (S3). Only
   *  `text`/`done` are read; `pending` bookkeeping stays internal to the buffer. */
  buffer: Pick<ToolArgsBuffer, "text" | "done">;
  /** The artifact kind once known ("" until the args name it). `html`/`svg` get the live preview
   *  frame; everything else shows the placeholder only. */
  kind?: string;
  /** The artifact title once known; a kind-derived name stands in until then. */
  title?: string;
  /** The handover (S8c): supplied by the caller ONCE it holds the real artifact-ref — so it is
   *  absent until the artifact has been created. When present AND the stream is `done`, this
   *  component renders `renderFinal(height)` — the real inline frame (S7b) — IN PLACE of the
   *  preview, passing the last measured preview height so the slot keeps its size across the swap. */
  renderFinal?: (height: number) => ReactNode;
};

export function StreamingPreview({ buffer, kind = "", title = "", renderFinal }: StreamingPreviewProps) {
  const name = refName({ title, kind });
  const processed = useMemo(() => processPartialHtml(buffer.text), [buffer.text]);

  // `previewGateOpen` is a point-in-time predicate and can flip back to false mid-stream (a later
  // still-opening `<style>` zeroes the byte count before it closes). LATCH it here: once the preview
  // has opened for this stream it stays open for the life of this card. Without the latch the frame
  // would unmount on the retract and remount when the gate re-opened, and the markup posted to the
  // remounted-but-not-yet-loaded frame would be lost — blanking the preview if that was the final
  // chunk. Setting state during render (guarded, so it runs at most once) is React's sanctioned way
  // to derive state from props across renders; it re-renders before commit, so there is no flash.
  const gateOpenNow = useMemo(() => previewGateOpen(buffer.text), [buffer.text]);
  const [gateLatched, setGateLatched] = useState(false);
  if (gateOpenNow && !gateLatched) setGateLatched(true);
  const showPreview = PREVIEW_KINDS.has(kind) && (gateLatched || gateOpenNow);

  // One random nonce per mounted frame; the srcdoc (and thus the CSP) are built once from it.
  const [nonce] = useState(randomNonce);
  const srcdoc = useMemo(() => buildPreviewSrcdoc(nonce), [nonce]);

  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const [frameLoaded, setFrameLoaded] = useState(false);

  // The frame's last reported content height, clamped to the inline-frame bounds — null until it
  // first measures. Sizes the preview frame and seeds the handover so the final inline frame
  // starts at the same height (no layout jump).
  const [measuredHeight, setMeasuredHeight] = useState<number | null>(null);

  // If the preview is ever torn down (e.g. the kind changes away from html/svg), drop the loaded
  // flag so any future frame must fire its own load before we post — markup can never land on a
  // remounted-but-unwired frame. The latch above means this does not fire during a live stream.
  useEffect(() => {
    if (!showPreview) setFrameLoaded(false);
  }, [showPreview]);

  // Size the preview from the height the frame reports after each morph (proto-preview:height),
  // gated on `e.source` being THIS frame's own opaque-origin window — the whole gate, as the
  // payload is one int we re-clamp host-side to [80,1200] anyway.
  useEffect(() => {
    if (!showPreview) return;
    const onMessage = (e: MessageEvent) => {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return;
      const d = (e.data || {}) as { type?: unknown; height?: unknown };
      if (d.type !== PREVIEW_HEIGHT_TYPE) return;
      const h = typeof d.height === "number" ? d.height : NaN;
      setMeasuredHeight(clampHeight(h));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [showPreview]);

  // Post the processed markup, THROTTLED to one post per second (OIU renderer.tsx) with an
  // IMMEDIATE flush on the milestones where a stale frame reads worst: the first `<style>` closing,
  // the first body markup appearing, and the stream's `done`. The load flag gates the first post so
  // it never lands before the frame's receiver is wired. A trailing post is scheduled once per
  // window and NOT rescheduled by later chunks (throttle, not debounce), so a chatty stream still
  // advances every second rather than stalling until it pauses.
  const lastPostAtRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingHtmlRef = useRef<string | null>(null);
  const milestoneRef = useRef({ styleClosed: false, body: false });
  useEffect(() => {
    if (!showPreview || !frameLoaded) return;
    pendingHtmlRef.current = processed;
    const flush = () => {
      timerRef.current = null;
      lastPostAtRef.current = Date.now();
      const html = pendingHtmlRef.current;
      pendingHtmlRef.current = null;
      if (html !== null) postMarkup(frameRef.current?.contentWindow, html);
    };
    // Milestones — each fires at most once per stream, save `done` which always flushes.
    const styleClosedNow = firstStyleClosed(buffer.text);
    const bodyNow = processed.length > 0;
    const milestone =
      buffer.done ||
      (styleClosedNow && !milestoneRef.current.styleClosed) ||
      (bodyNow && !milestoneRef.current.body);
    if (styleClosedNow) milestoneRef.current.styleClosed = true;
    if (bodyNow) milestoneRef.current.body = true;

    const sinceLast = Date.now() - lastPostAtRef.current;
    if (milestone || sinceLast >= PREVIEW_THROTTLE_MS) {
      if (timerRef.current) clearTimeout(timerRef.current);
      flush();
      return;
    }
    // Within the throttle window: schedule ONE trailing post for the window's end; later chunks
    // only refresh `pendingHtmlRef` (above) so the trailing post carries the newest markup.
    if (timerRef.current === null) {
      timerRef.current = setTimeout(flush, PREVIEW_THROTTLE_MS - sinceLast);
    }
  }, [showPreview, frameLoaded, processed, buffer.text, buffer.done]);

  // Never leave a trailing post timer behind on unmount.
  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  // Handover (S8c): once the stream is `done` and the caller has handed us the real artifact-ref
  // (`renderFinal`), swap the sandboxed preview for the artifact's own inline frame (S7b), seeded
  // with the last measured height so the slot keeps its size. Placed after every hook so the hook
  // order is stable across the swap.
  const lastHeight = measuredHeight ?? PREVIEW_FRAME_HEIGHT;
  if (buffer.done && renderFinal) {
    return <>{renderFinal(lastHeight)}</>;
  }

  return (
    <div
      className="artifact-ref-inline artifact-ref-inline--streaming"
      data-testid="streaming-preview"
      data-kind={kind || undefined}
      data-preview={showPreview ? "true" : undefined}
    >
      <div className="artifact-ref-inline__head">
        <Sparkles size={14} aria-hidden className="code-ref-chip__icon" />
        <span className="artifact-ref-chip__title">{name}</span>
        <span className="artifact-ref-inline__spacer" />
        <span className="artifact-ref-inline__status" data-testid="streaming-preview-status" role="status">
          {statusLine(kind, buffer.done)}
        </span>
      </div>
      {showPreview ? (
        <iframe
          ref={frameRef}
          className="artifact-ref-inline__frame artifact-ref-inline__preview-frame"
          data-testid="streaming-preview-frame"
          title={name}
          // allow-scripts ONLY — never allow-same-origin, so the frame is opaque-origin and cannot
          // read the console's credentials. The markup is injected via postMessage, never the URL.
          sandbox="allow-scripts"
          srcDoc={srcdoc}
          style={{ height: lastHeight, width: "100%", border: 0 }}
          onLoad={() => setFrameLoaded(true)}
        />
      ) : (
        <div
          className="artifact-ref-inline__body artifact-ref-inline__placeholder"
          data-testid="streaming-preview-placeholder"
          style={{ height: MIN_FRAME_HEIGHT }}
        />
      )}
    </div>
  );
}
