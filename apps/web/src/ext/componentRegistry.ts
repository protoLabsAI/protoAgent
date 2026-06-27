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

const _renderers: Record<string, ChatComponentRenderer> = {};

/**
 * Register an inline chat-component renderer for `name` (the component-v1 kind the agent
 * passes to `show_component`). Last registration of a name wins, so a fork/plugin can both
 * ADD new kinds and OVERRIDE a built-in. Returns an unregister fn (HMR-friendly).
 */
export function registerChatComponent(name: string, render: ChatComponentRenderer): () => void {
  const key = (name || "").trim();
  if (!key || typeof render !== "function") return () => {};
  _renderers[key] = render;
  return () => {
    if (_renderers[key] === render) delete _renderers[key];
  };
}

/** The registered renderers, keyed by component-v1 kind. Merged over the built-ins. */
export function registeredChatComponents(): Record<string, ChatComponentRenderer> {
  return _renderers;
}
