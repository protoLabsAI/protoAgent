// Core's first DYNAMIC ⌘K source (ADR 0061): one palette row per OPEN CHAT TAB, so the
// operator switches to a chat BY NAME instead of by ordinal.
//
// The gap this closes: ⌘1–9 (`chat.tab.N`, coreKeybindings.ts) jumps to a tab by POSITION,
// and nothing in the console surfaces a session's title outside the tab strip itself — so
// with more than a handful of chats open, "go back to the one about the release notes"
// means reading the strip and counting. The palette is where you type a name.
//
// Why `registerPaletteSource` and not `registerPaletteCommand`: sessions are live. Up to
// MAX_SESSIONS (50) of them, created and deleted while the console runs, and a title is
// DERIVED from the first user message (`titleFromMessages`) — so it changes after a chat's
// first turn. A static registration is a snapshot: it would list the tab the operator just
// closed, miss the one they just opened, and label a titled chat "New chat" forever.
// Nothing observes the chat store on the seam's behalf (`paletteCommandsVersion()` moves
// only on register/unregister), so a snapshot would go stale silently and STAY stale. A
// source is re-invoked on every palette read — on open and on every keystroke — which is
// exactly the freshness these rows need.
//
// A source is therefore on the hot path of typing, so this one is CHEAP and SYNCHRONOUS:
// one `chatStore.getSnapshot()` read and a map over ≤50 sessions. It never subscribes and
// never writes — a store write from inside a palette read would re-enter the render that is
// reading it.
import { chatStore, DEFAULT_SESSION_TITLE } from "../chat/chat-store";
import { registerPaletteSource } from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { navigate } from "./usePaletteRegistry";
import type { NavIntent } from "./usePaletteRegistry";

/** Palette group for the chat rows. Its own group so they read as CHATS — a list of the
 *  operator's conversations — rather than as more commands in the Commands group. The DS
 *  commands view renders a header wherever the group changes, and provider rows are
 *  appended after the statics, so these land as one labelled block at the end. */
export const CHAT_TAB_GROUP = "Chats";

/** Row id prefix — namespaced so a session id can never collide with a static command's
 *  id (a static wins the seam's dedup, and would shadow the live row). */
export const CHAT_TAB_ID_PREFIX = "chat.tab:";

/** How many tab positions have a `chat.tab.N` keybinding to advertise (⌘1–⌘9). */
const ORDINAL_BINDINGS = 9;

/** Keywords every chat row carries, so the operator reaches the whole group by typing what
 *  it IS rather than what it is called. `label` already carries the title, and the matcher
 *  is substring-over-the-joined-haystack, so these are what the title itself can't supply. */
const CHAT_KEYWORDS = ["chat", "tab", "session", "conversation", "switch", "go to"];

/** Title words as individual keywords. Redundant with the label for a plain substring
 *  match — kept because it is the field a matcher would use for per-word scoring, and it
 *  costs one split. Non-ASCII titles yield no words here; they stay searchable through the
 *  label, which is the whole title verbatim. */
function titleKeywords(title: string): string[] {
  const words = title.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 1);
  return [...new Set(words)].slice(0, 12);
}

/** The rows for the CURRENT chat-store snapshot, in tab order (left→right, the same order
 *  ⌘1–9 counts in). Exported for tests; the registered source below just calls it.
 *
 *  No cap. MAX_SESSIONS already bounds this at 50, the mapping is trivial, and any cap
 *  would need an order to cap BY — capping in tab order silently drops the newest tabs
 *  (new sessions append right), and capping by recency is ranking, which belongs to the
 *  root-view work, not here. The empty-query root list is that PR's problem to shorten; a
 *  typed query is exactly when the operator wants every match, capped by nothing. */
export function chatTabPaletteRows(
  nav: (intent: NavIntent) => void = navigate,
): PaletteCommand[] {
  const { sessions, currentSessionId } = chatStore.getSnapshot();
  return sessions.map((session, i) => {
    // A blank title would render an unreadable empty row. `DEFAULT_SESSION_TITLE` is the
    // honest fallback — it is what the tab strip shows for a chat that has not been named
    // by its first message yet, so the row matches what the operator is looking at.
    const title = session.title?.trim() || DEFAULT_SESSION_TITLE;
    const isCurrent = session.id === currentSessionId;
    return {
      id: `${CHAT_TAB_ID_PREFIX}${session.id}`,
      label: title,
      group: CHAT_TAB_GROUP,
      keywords: [...titleKeywords(title), ...CHAT_KEYWORDS],
      // The chat you are already on stays RUNNABLE rather than `disabled`: from Knowledge,
      // Memory, or any other surface its row is a real navigation ("show me the chat"), so
      // disabling it would kill the action exactly where it earns its place. The hint says
      // where you are without pretending the row is dead.
      hint: isCurrent ? "current" : undefined,
      // Positions 1–9 ADVERTISE their `chat.tab.N` binding, which is the other half of
      // closing this gap: the row teaches which title ⌘1 actually jumps to. `keybinding` is
      // a binding ID, never a literal combo — the host renders the LIVE combo, so the row
      // keeps telling the truth after the operator rebinds it in Settings ▸ Keyboard. (Those
      // bindings are `scope: "chat"`, so the combo fires while focus is in the chat panel;
      // the palette row itself works from anywhere.) An explicit `hint` wins over the combo,
      // so the current row shows "current" instead — where the shortcut is moot anyway.
      keybinding: !isCurrent && i < ORDINAL_BINDINGS ? `chat.tab.${i + 1}` : undefined,
      run: (ctx) => {
        // Through the NavIntent chokepoint, NOT `chatStore.switchSession` directly: the
        // frameless desktop launcher mounts this same registry in a shell-less context and
        // forwards intents to the main window, where the store that owns the tabs lives.
        // The intent is also where the id is re-validated — see `applyNavIntent`.
        nav({ kind: "chat", sessionId: session.id });
        ctx.close();
      },
    };
  });
}

/** Register the chat-tab rows as a palette source. Returns the unregister fn. */
export function registerChatTabPaletteSource(
  nav: (intent: NavIntent) => void = navigate,
): () => void {
  return registerPaletteSource(() => chatTabPaletteRows(nav));
}

// Self-register on import, the way core keybindings do (`keybindings/index.ts`). BOTH palette
// hosts pull this module in for the side effect — App (the console window) and Launcher (the
// frameless desktop window), which is why the rows navigate by intent rather than by touching
// the store. The adapter (`usePaletteRegistry`) deliberately does NOT import it: the adapter
// knows nothing about chat tabs, and keeping it out of that module's import graph keeps the
// seam's own "no source registered ⇒ no provider wired" guarantee testable.
//
// Registration is unconditional and permanent: the chat store always holds at least one
// session (`loadPersisted` creates one when storage is empty), so there is never a "no rows"
// state worth withdrawing for.
registerChatTabPaletteSource();
