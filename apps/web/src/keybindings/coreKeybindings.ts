// Core default keybindings (ADR 0063), registered through the SAME `registerKeybinding`
// seam a fork uses — imported for side effects by App. Actions go through stores/intents so
// they need no React context.
//
// Scoping (decided with the user): palette/settings/focus-composer are GLOBAL; the chat
// tab/new/clear ops are scoped to the chat panel (`scope: "chat"`, active only when focus is
// within the chat surface) and `allowInInput` so they fire while typing in the composer.
//
// NOTE: ⌘T / ⌘1–9 / ⌃Tab are browser-reserved — they work in the Tauri desktop app but a
// plain browser tab swallows them. Because every binding is rebindable, browser users can
// remap to non-reserved combos in Settings ▸ Keyboard.
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
  defaultKeys: "mod+t",
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
  defaultKeys: "ctrl+tab",
  scope: "chat",
  allowInInput: true,
  run: () => switchByOffset(1),
});
registerKeybinding({
  id: "chat.tab.prev",
  label: "Previous chat tab",
  group: "Chat",
  defaultKeys: "ctrl+shift+tab",
  scope: "chat",
  allowInInput: true,
  run: () => switchByOffset(-1),
});
for (let n = 1; n <= 9; n++) {
  registerKeybinding({
    id: `chat.tab.${n}`,
    label: `Jump to chat tab ${n}`,
    group: "Chat",
    defaultKeys: `mod+${n}`,
    scope: "chat",
    allowInInput: true,
    run: () => switchToIndex(n - 1),
  });
}

// ── Panels (global toggles) ──────────────────────────────────────────────────────────
// VS Code-style: ⌘B left rail, ⌘⌥B right panel, ⌘J bottom dock. Same desktop-vs-browser
// trade-off as the chat ops (⌘B/⌘J are browser shortcuts in a tab) — work in the desktop
// app, rebindable in a browser.
const toggle = (get: () => boolean, set: (b: boolean) => void) => () => set(!get());
registerKeybinding({
  id: "panel.toggle.left",
  label: "Toggle left rail",
  group: "Panels",
  defaultKeys: "mod+b",
  allowInInput: true,
  run: toggle(() => useUI.getState().leftCollapsed, (b) => useUI.getState().setLeftCollapsed(b)),
});
registerKeybinding({
  id: "panel.toggle.right",
  label: "Toggle right panel",
  group: "Panels",
  defaultKeys: "mod+alt+b",
  allowInInput: true,
  run: toggle(() => useUI.getState().rightCollapsed, (b) => useUI.getState().setRightCollapsed(b)),
});
registerKeybinding({
  id: "panel.toggle.bottom",
  label: "Toggle bottom dock",
  group: "Panels",
  defaultKeys: "mod+j",
  allowInInput: true,
  run: toggle(() => useUI.getState().bottomCollapsed, (b) => useUI.getState().setBottomCollapsed(b)),
});

// ── Focus a dock (global) ────────────────────────────────────────────────────────────
// Ctrl+1–4 move keyboard FOCUS into a region (so that region's scoped binds activate) —
// distinct from ⌘1–9, which JUMP chat tabs. Literal `ctrl` (not `mod`): on mac that's a
// different key from ⌘, so it never collides with ⌘1–9. (On Win/Linux `mod`=Ctrl, so
// ctrl+1 overlaps ⌘1's tab-jump — the conflict detector flags it; rebind there.)
function focusDock(colSelector: string): void {
  const col = document.querySelector(colSelector);
  if (!col) return;
  // Land on the first interactive element so the user can act immediately; fall back to the
  // column itself (made programmatically focusable) so the keyboard scope still activates.
  const focusable = col.querySelector<HTMLElement>(
    'input:not([disabled]), textarea:not([disabled]), button:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
  );
  if (focusable) {
    focusable.focus();
    return;
  }
  const el = col as HTMLElement;
  if (el.tabIndex < 0) el.tabIndex = -1;
  el.focus();
}
registerKeybinding({
  id: "focus.chat",
  label: "Focus chat composer",
  group: "Focus",
  defaultKeys: "ctrl+1",
  allowInInput: true,
  run: () => useKbIntents.getState().focusComposer(),
});
registerKeybinding({
  id: "focus.left",
  label: "Focus left panel",
  group: "Focus",
  defaultKeys: "ctrl+2",
  allowInInput: true,
  run: () => focusDock(".pl-appshell__col--left"),
});
registerKeybinding({
  id: "focus.right",
  label: "Focus right panel",
  group: "Focus",
  defaultKeys: "ctrl+3",
  allowInInput: true,
  run: () => focusDock(".pl-appshell__col--right"),
});
registerKeybinding({
  id: "focus.bottom",
  label: "Focus bottom dock",
  group: "Focus",
  defaultKeys: "ctrl+4",
  allowInInput: true,
  run: () => focusDock(".pl-appshell__bottom"),
});
