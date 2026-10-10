import { Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { ToolArgsBuffer } from "../chat/toolArgsBuffer";
import { refName } from "./artifactRef";
import { MIN_FRAME_HEIGHT } from "./inlineFrames";
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
// SCOPE of this slice: post the processed markup on EVERY update. Throttling to 1/s with milestone
// flushes, and the handover that swaps this preview for the real inline frame (D2) keeping the last
// measured height, are S8c — so the frame here uses a fixed preview height and does not yet read a
// height message back.

/** The kinds that get a live preview frame; every other kind shows the placeholder only. */
const PREVIEW_KINDS = new Set(["html", "svg"]);

/** The preview frame's root node — the morph receiver targets it; the host builds it empty. */
const PREVIEW_ROOT_ID = "proto-preview-root";

/** The postMessage type the host sends and the in-frame receiver listens for. */
const PREVIEW_MORPH_TYPE = "proto-preview:morph";

/** The preview frame's fixed height for this slice. S8c drives it from a reported height and hands
 *  the measured value over to the final inline frame so the transcript layout does not jump. */
const PREVIEW_FRAME_HEIGHT = 240;

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
 *  the processed markup so the preview updates in place instead of being rebuilt. Built from the
 *  module constants so the host and the frame agree on the type and the root id. */
const MORPH_RECEIVER = [
  "(function(){",
  "function render(html){",
  `var root=document.getElementById(${JSON.stringify(PREVIEW_ROOT_ID)});`,
  "if(!root)return;",
  'try{Idiomorph.morph(root,html,{morphStyle:"innerHTML"});}catch(_){root.innerHTML="";}',
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
};

export function StreamingPreview({ buffer, kind = "", title = "" }: StreamingPreviewProps) {
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

  // If the preview is ever torn down (e.g. the kind changes away from html/svg), drop the loaded
  // flag so any future frame must fire its own load before we post — markup can never land on a
  // remounted-but-unwired frame. The latch above means this does not fire during a live stream.
  useEffect(() => {
    if (!showPreview) setFrameLoaded(false);
  }, [showPreview]);

  // Post the processed markup on every update once the frame is mounted and has loaded. The load
  // flag gates the first post so it does not land before the frame's receiver is wired. Throttle,
  // milestone flush and height handover are S8c; here every change posts.
  useEffect(() => {
    if (!showPreview || !frameLoaded) return;
    postMarkup(frameRef.current?.contentWindow, processed);
  }, [showPreview, frameLoaded, processed]);

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
          style={{ height: PREVIEW_FRAME_HEIGHT, width: "100%", border: 0 }}
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
