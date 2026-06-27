// Core default keybindings (ADR 0063), registered through the SAME `registerKeybinding`
// seam a fork uses — imported for side effects by App. Actions go through stores/intents so
// they need no React context.
//
// Scoping (decided with the user): palette/settings/focus-composer are GLOBAL; the chat
// tab/new/clear ops are scoped to the chat panel (`scope: "chat"`, active only when focus is
// within the chat surface) and `allowInInput` so they fire while typing in the composer.
//
// The chat ops use ⌘⌃ (Command+Control) combos: plain ⌘T/⌘N/⌘1–9/⌃Tab are browser-reserved
// (new tab / new window / tab-switch), so the browser eats them before the page sees them.
// ⌘⌃ is not browser-reserved, so these work in both the browser console and the desktop app.
import { api } from "../lib/api";
import { chatStore } from "../chat/chat-store";
import { useUI } from "../state/uiStore";
import { registerKeybinding } from "../ext/keybindingRegistry";
import { useKbIntents } from "./intents";

function switchByOffset(delta: number): void {
  const { sessions, currentSessionId } = chatStore.getSnapshot();
  if (sessions.length === 0) return;
  const cur = sessions.findIndex((s) => s.id === currentSessionId);
  const base = cur < 0 ? 0 : cur;
  const next = sessions[(base + delta + sessions.length) % sessions.length];
  if (next) chatStore.switchSession(next.id);
}

function switchToIndex(i: number): void {
  const target = chatStore.getSnapshot().sessions[i];
  if (target) chatStore.switchSession(target.id);
}

// ── Global ────────────────────────────────────────────────────────────────────────
registerKeybinding({
  id: "palette.toggle",
  label: "Command palette",
  group: "General",
  defaultKeys: "mod+k",
  allowInInput: true,
  run: () => useKbIntents.getState().togglePalette(),
});
registerKeybinding({
  id: "settings.open",
  label: "Open Settings",
  group: "General",
  defaultKeys: "mod+,",
  allowInInput: true,
  run: () => useUI.getState().openGlobalSettings(),
});
registerKeybinding({
  id: "composer.focus",
  label: "Focus chat composer",
  group: "General",
  defaultKeys: "/", // plain key → only fires when NOT already typing in a field
  run: () => useKbIntents.getState().focusComposer(),
});

// ── Chat panel (scope: "chat") ──────────────────────────────────────────────────────
registerKeybinding({
  id: "chat.new",
  label: "New chat",
  group: "Chat",
  defaultKeys: "mod+ctrl+n", // ⌘⌃N — ⌘T/⌘N are browser-reserved; ⌘⌃ escapes the browser
  scope: "chat",
  allowInInput: true,
  run: () => chatStore.createSession(),
});
registerKeybinding({
  id: "chat.clear",
  label: "Clear conversation",
  group: "Chat",
  defaultKeys: "mod+shift+k",
  scope: "chat",
  allowInInput: true,
  run: () => {
    const { currentSessionId } = chatStore.getSnapshot();
    if (!currentSessionId) return;
    void api.deleteChatSession(currentSessionId, false).catch(() => {});
    chatStore.updateMessages(currentSessionId, []);
  },
});
registerKeybinding({
  id: "chat.tab.next",
  label: "Next chat tab",
  group: "Chat",
  defaultKeys: "mod+ctrl+tab", // ⌘⌃Tab — plain ⌃Tab is the browser's tab-switch
  scope: "chat",
  allowInInput: true,
  run: () => switchByOffset(1),
});
registerKeybinding({
  id: "chat.tab.prev",
  label: "Previous chat tab",
  group: "Chat",
  defaultKeys: "mod+ctrl+shift+tab",
  scope: "chat",
  allowInInput: true,
  run: () => switchByOffset(-1),
});
for (let n = 1; n <= 9; n++) {
  registerKeybinding({
    id: `chat.tab.${n}`,
    label: `Jump to chat tab ${n}`,
    group: "Chat",
    defaultKeys: `mod+ctrl+${n}`, // ⌘⌃1–9 — plain ⌘1–9 are the browser's tab-switch
    scope: "chat",
    allowInInput: true,
    run: () => switchToIndex(n - 1),
  });
}
