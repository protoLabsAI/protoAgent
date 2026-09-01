import type { PluginView as PluginViewMeta } from "../lib/types";
import type { ExtSurface } from "../ext";

import { registeredSurfaces } from "../ext";
import { ChatSurface } from "../chat/ChatSurface";
import { ErrorBoundary, PanelError } from "./ErrorBoundary";
import { PluginView } from "./PluginView";

/** Who provides the chat slot in THIS window. Exported because two readers have to agree and
 *  drifting apart would be silent: `ChatSlot` renders the winner, and the command palette
 *  asks whether the winner is the BUILT-IN surface before offering the chat's slash commands
 *  — those rows dispatch through `slashDispatch`, a seam only the built-in surface publishes,
 *  so a fork surface or a plugin iframe holding the slot means the rows have nothing to run
 *  against. */
export type ChatSlotProvider = "fork" | "plugin" | "builtin";

/** The fork surface claiming the chat slot, if any (ADR 0045, resolution step 1). */
function chatSlotFork(enabledPluginIds: Set<string>): ExtSurface | undefined {
  return registeredSurfaces().find(
    (s) => s.id === "chat" && (!s.requiresPlugin || enabledPluginIds.has(s.requiresPlugin)),
  );
}

/** Resolve the slot the way `ChatSlot` renders it: a fork `registerSurface` wins, then an
 *  enabled plugin view declaring `slot: "chat"`, then the built-in surface — the console is
 *  never chat-less, so this always answers.
 *
 *  Deliberately NOT a question about whether anything is MOUNTED. A collapsed dock is
 *  unmounted by the DS AppShell (it renders `leftContent` only while the dock is open), so
 *  "this window has a built-in chat" and "the chat slot is registered right now" are
 *  different facts, and only this one survives the operator hiding the panel. */
export function chatSlotProvider(
  enabledPluginIds: Set<string>,
  pluginView?: PluginViewMeta | undefined,
): ChatSlotProvider {
  if (chatSlotFork(enabledPluginIds)) return "fork";
  return pluginView ? "plugin" : "builtin";
}

// The chat surface is a SLOT, not a hardcoded panel (ADR 0045). Resolution order:
//   1. a fork surface registered with id "chat" (src/ext seam — in-process React),
//   2. an enabled plugin view declaring `slot: "chat"` (sandboxed iframe),
//   3. the built-in ChatSurface (the default; the console is never chat-less).
//
// Whatever provides the slot inherits chat's mount contract: it is rendered for the
// app's LIFETIME and `active` only toggles visibility (#613 — unmounting mid-turn
// loses the in-flight stream). The built-in surface implements that itself; override
// providers are wrapped (display:contents keeps the wrapper out of the layout).
// The contract covers the SURFACE SWITCH, not the dock: collapsing the dock this slot
// lives on unmounts it outright (see `chatSlotProvider`).
export function ChatSlot({
  active,
  onError,
  pluginView,
  enabledPluginIds,
}: {
  active: boolean;
  onError: (message: string) => void;
  pluginView?: (PluginViewMeta & { key: string }) | undefined;
  enabledPluginIds: Set<string>;
}) {
  const ext = chatSlotFork(enabledPluginIds);
  if (ext) {
    return (
      <div className="chat-slot" style={{ display: active ? "contents" : "none" }}>
        {/* Fork-registered surfaces are arbitrary code — a throw must stay contained
            in the slot, not unmount the app (#872). */}
        <ErrorBoundary
          fallback={({ error, reset }) => <PanelError error={error} reset={reset} label="chat surface" />}
        >
          {ext.render()}
        </ErrorBoundary>
      </div>
    );
  }
  if (pluginView) {
    return (
      <div className="chat-slot" style={{ display: active ? "contents" : "none" }}>
        <PluginView view={pluginView} />
      </div>
    );
  }
  return <ChatSurface onError={onError} active={active} />;
}
