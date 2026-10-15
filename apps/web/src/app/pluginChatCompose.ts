// `protoagent:chat:compose` — a plugin console view hands the operator a half-written chat
// message ("Ask the agent about this element"). PluginView calls this only for a message from
// its OWN iframe at the plugin page's origin (the same gate every other bridge message passes).
//
// The contract is DRAFT, never send. The text lands at the front of the visible chat
// composer's draft (the same `prefillChatDraft` seam the ⌘⇧K skill rows use — chat/slashDispatch),
// the chat surface is raised if it was hidden or its dock collapsed (`onChatReady`, shared with
// those rows), the textarea takes focus, and the operator reads it and presses Enter.
//
// Why there is no `send: true`. The artifact shell's `protoArtifact:send` does post a turn, but
// only through a HOST-owned confirm chip (ADR 0118 D4) — the frame proposes, the operator
// decides. A plugin view has no such chip, and "only with a user gesture inside the view" can't
// be enforced from here: transient user activation is a ~5 s window shared by the whole frame
// tree, it proves the operator clicked *something* (a list row, a scrollbar), not that they
// consented to *this* text, and the host can't see what the click was on. A same-origin plugin
// page could also synthesize the gesture's aftermath anyway. So `send` is not part of the
// contract — the field is ignored and the message is always a prefill. The composer's own
// Enter is the operator gesture.
//
// What a plugin can't do: target another session or agent (any such field is ignored — the
// draft always lands in the chat the operator is looking at), post rich content (plain text;
// control characters are stripped), or post more than MAX_COMPOSE_BYTES. Provenance: the
// composer has no per-draft origin chip, so the host announces "Drafted from ‹plugin›" in a
// toast the moment the text lands — the operator learns where it came from before sending.

import { prefillChatDraft } from "../chat/slashDispatch";
import { onChatReady, type Navigate } from "./chatSlashPalette";

/** The cap on a compose payload, in UTF-8 bytes. A prompt a view hands over is a question
 *  about one element, not a document — and an over-cap payload is REFUSED, never truncated, so
 *  the operator never sends a silently clipped prompt. */
export const MAX_COMPOSE_BYTES = 4096;

/** Minimum gap between two composes from the same view. Each one raises chat and takes focus,
 *  so a view in a loop would otherwise yank the operator off whatever they're doing. */
export const COMPOSE_MIN_INTERVAL_MS = 1000;

export type PluginChatCompose = { text: string };

// C0 controls except \t and \n, DEL, and C1 controls — invisible in the composer, and nothing
// a prompt needs. \r\n / \r are normalized to \n first.
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F]/g;

/** A `protoagent:chat:compose` payload → `{text}`, or null when it isn't one (wrong type,
 *  non-string or blank text, or over MAX_COMPOSE_BYTES after normalization). Every other field
 *  — `send`, `sessionId`, `agent`, … — is ignored by construction. */
export function parsePluginChatCompose(m: unknown): PluginChatCompose | null {
  if (!m || typeof m !== "object") return null;
  const o = m as Record<string, unknown>;
  if (o.type !== "protoagent:chat:compose") return null;
  if (typeof o.text !== "string") return null;
  const text = o.text.replace(/\r\n?/g, "\n").replace(CONTROL, "").trim();
  if (!text) return null;
  if (new TextEncoder().encode(text).length > MAX_COMPOSE_BYTES) return null;
  return { text };
}

export type ComposeDeps = {
  /** Raise chat (if needed), make sure there's a session, then run `act`; `onGiveUp` when no
   *  composer ever appears. Defaults to `onChatReady` + the console's own navigate. */
  ready: (act: () => void, onGiveUp: () => void) => void;
  /** Write the draft + focus. Defaults to the chat seam's `prefillChatDraft`. */
  prefill: (text: string) => boolean;
  /** Toast sink (null when no provider is mounted). */
  notify: ((t: { tone: "info" | "warning"; title: string; message: string }) => void) | null;
  now: () => number;
};

export function defaultComposeDeps(navigate: Navigate, notify: ComposeDeps["notify"]): ComposeDeps {
  return {
    ready: (act, onGiveUp) => onChatReady(act, navigate, onGiveUp),
    prefill: prefillChatDraft,
    notify,
    now: () => Date.now(),
  };
}

/** A per-view composer: `compose(raw)` parses, throttles and routes one message. Returns
 *  whether the message was accepted for routing (false = not a compose, malformed, over the
 *  cap, or throttled) — the landing itself is asynchronous and reported by toast. */
export function createPluginChatComposer(pluginLabel: string, deps: ComposeDeps) {
  let last = -Infinity;
  return function compose(raw: unknown): boolean {
    const req = parsePluginChatCompose(raw);
    if (!req) return false;
    const t = deps.now();
    if (t - last < COMPOSE_MIN_INTERVAL_MS) return false;
    last = t;
    deps.ready(
      () => {
        if (deps.prefill(req.text)) {
          deps.notify?.({ tone: "info", title: "Drafted in chat", message: `From ${pluginLabel} — review it, then send.` });
        } else {
          giveUp();
        }
      },
      giveUp,
    );
    return true;
  };

  function giveUp() {
    deps.notify?.({
      tone: "warning",
      title: "Couldn't draft in chat",
      message: `${pluginLabel} asked to draft a message, but there's no chat composer to put it in.`,
    });
  }
}
