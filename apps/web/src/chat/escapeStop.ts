// Escape-to-stop keybinding action (ADR 0063, #2968). The `chat.stop` binding (Escape,
// chat-scoped) matches the Claude.ai/ChatGPT convention: while a turn streams, Escape
// stops it; with steers queued into the running turn, Escape first peels off the MOST
// RECENT queued steer (LIFO) — one per press — and only stops the turn once none remain.
// When idle it is a strict no-op: destructive Escape (clearing the draft, blurring the
// composer) is an antipattern.
//
// The stream/steer state lives inside the visible ChatSessionSlot (component state +
// refs), which the keybinding registry can't see — bindings run outside React. So the
// slot publishes an imperative handler here (same last-write-wins + guarded-unregister
// shape as `registerKeybinding`) and the binding's `run` just invokes it. Only the
// VISIBLE slot registers, so the handler always targets the session the user is looking
// at. The slash-menu Escape (dismiss popover) never reaches this path: the composer's
// React onKeyDown preventDefaults it and the global keydown host skips defaultPrevented
// events — no double-fire.

import type { SessionStatus } from "./chat-store";

export type EscapeAction =
  | { kind: "cancel-steer"; steerId: string }
  | { kind: "stop" }
  | { kind: "none" };

/** Pure decision for one Escape press: streaming with queued steers → cancel the newest
 *  (LIFO; successive presses peel one at a time); streaming with none → stop the turn;
 *  not streaming → no-op (never touch the draft or focus). */
export function resolveEscapeAction(
  status: SessionStatus,
  steerQueue: readonly { id: string }[],
): EscapeAction {
  if (status !== "streaming") return { kind: "none" };
  const newest = steerQueue[steerQueue.length - 1];
  if (newest) return { kind: "cancel-steer", steerId: newest.id };
  return { kind: "stop" };
}

export type ChatEscapeHandler = () => void;

let escapeHandler: ChatEscapeHandler | null = null;

/** Publish the visible slot's Escape behavior. Last write wins (the newly visible slot
 *  simply replaces the outgoing one); the returned unregister only clears its OWN
 *  handler, so cleanup ordering across slot re-renders can't drop a fresh registration. */
export function registerChatEscapeHandler(handler: ChatEscapeHandler): () => void {
  escapeHandler = handler;
  return () => {
    if (escapeHandler === handler) escapeHandler = null;
  };
}

/** Keybinding action (`chat.stop`): run the visible slot's Escape behavior. No slot
 *  registered (chat surface not mounted) ⇒ no-op. */
export function runChatEscape(): void {
  escapeHandler?.();
}
