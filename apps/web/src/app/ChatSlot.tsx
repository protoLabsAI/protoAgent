import type { PluginView as PluginViewMeta } from "../lib/types";

import { registeredSurfaces } from "../ext";
import { ChatSurface } from "../chat/ChatSurface";
import { PluginView } from "./PluginView";

// The chat surface is a SLOT, not a hardcoded panel (ADR 0045). Resolution order:
//   1. a fork surface registered with id "chat" (src/ext seam — in-process React),
//   2. an enabled plugin view declaring `slot: "chat"` (sandboxed iframe),
//   3. the built-in ChatSurface (the default; the console is never chat-less).
//
// Whatever provides the slot inherits chat's mount contract: it is rendered for the
// app's LIFETIME and `active` only toggles visibility (#613 — unmounting mid-turn
// loses the in-flight stream). The built-in surface implements that itself; override
// providers are wrapped (display:contents keeps the wrapper out of the layout).
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
  const ext = registeredSurfaces().find(
    (s) => s.id === "chat" && (!s.requiresPlugin || enabledPluginIds.has(s.requiresPlugin)),
  );
  if (ext) {
    return (
      <div className="chat-slot" style={{ display: active ? "contents" : "none" }}>
        {ext.render()}
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
