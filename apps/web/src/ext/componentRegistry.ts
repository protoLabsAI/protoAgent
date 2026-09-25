import type { JSX } from "react";

// Build-time fork/plugin seam for INLINE CHAT COMPONENTS (ADR 0051 / #1323, extends ADR 0061).
// A fork or first-party plugin drops a `src/ext/<name>.tsx` that calls
// `registerChatComponent()` to add a renderer for a component-v1 kind — so the agent's
// `show_component(<kind>, props)` tool can render a NEW widget WITHOUT editing
// `ChatComponent.tsx`, keeping `git pull upstream` conflict-free.
//
// This is the same shape as the AI SDK's per-tool renderers / CopilotKit's `useComponent`:
// a typed kind name → a client-registered React renderer fed pure-data `props`. Core ships
// table/keyvalue/timeline as built-ins; registered renderers extend that set (and a
// registered kind overrides a built-in of the same name — last-wins, so a fork can re-skin
// `table`). Data-only + curated, so it's safe inline (free-form code stays on the ADR 0038
// iframe/artifact path). Sibling of `registerComposerAction` / `registerSlashCommand`.

/** A renderer for one component-v1 kind: pure-data `props` → inline React. */
export type ChatComponentRenderer = (p: { props: Record<string, unknown> }) => JSX.Element;

/** A hook for a component that arrives on the LIVE turn stream (#3617) — never on history
 *  hydration, reattach or replay, where the same component only re-renders. The place for
 *  "the agent pointed at something just now" side effects, like opening the surface a chip
 *  points into. `sessionId` is the chat the turn belongs to (not necessarily the one on
 *  screen — a background tab streams too). */
export type LiveComponentHandler = (spec: { component: string; props: Record<string, unknown> }, ctx: { sessionId?: string }) => void;

export type ChatComponentOptions = { onLive?: LiveComponentHandler };

const _renderers: Record<string, ChatComponentRenderer> = {};
const _live: Record<string, LiveComponentHandler> = {};

/**
 * Register an inline chat-component renderer for `name` (the component-v1 kind the agent
 * passes to `show_component`, or a plugin tool emits). Last registration of a name wins, so a
 * fork/plugin can both ADD new kinds and OVERRIDE a built-in. `opts.onLive` runs when such a
 * component arrives on the live turn stream (see LiveComponentHandler). Returns an unregister
 * fn (HMR-friendly).
 */
export function registerChatComponent(
  name: string,
  render: ChatComponentRenderer,
  opts: ChatComponentOptions = {},
): () => void {
  const key = (name || "").trim();
  if (!key || typeof render !== "function") return () => {};
  _renderers[key] = render;
  const onLive = typeof opts.onLive === "function" ? opts.onLive : undefined;
  if (onLive) _live[key] = onLive;
  else delete _live[key]; // a re-registration without a hook must not keep the old one
  return () => {
    if (_renderers[key] === render) delete _renderers[key];
    if (onLive && _live[key] === onLive) delete _live[key];
  };
}

/** Called by the chat surface for each component on the LIVE stream (and only there). A
 *  throwing hook is contained — a plugin's side effect must never break the turn. */
export function dispatchLiveComponent(
  spec: { component: string; props: Record<string, unknown> },
  sessionId?: string,
): void {
  const hook = _live[spec.component];
  if (!hook) return;
  try {
    hook(spec, { sessionId });
  } catch {
    /* contained — see above */
  }
}

/** The registered renderers, keyed by component-v1 kind. Merged over the built-ins. */
export function registeredChatComponents(): Record<string, ChatComponentRenderer> {
  return _renderers;
}
