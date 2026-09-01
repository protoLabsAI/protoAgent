// Core's first DYNAMIC command-palette source (ADR 0061): one row per OPEN CHAT TAB, so the
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

/** Palette group for the chat rows. Its own group so they read as CHATS — a list of the
 *  operator's conversations — rather than as more commands in the Commands group. The DS
 *  commands view renders a header wherever the group changes, and provider rows are
 *  appended after the statics, so these land as one labelled block at the end. */
export const CHAT_TAB_GROUP = "Chats";

/** Row id prefix — namespaced so a session id can never collide with a static command's
 *  id (a static wins the seam's dedup, and would shadow the live row). */
export const CHAT_TAB_ID_PREFIX = "chat.tab:";

/** How many tab positions have a `chat.tab.N` keybinding to advertise (⌘1–⌘9, the loop at
 *  the end of coreKeybindings.ts). Pinned to that loop by a test rather than imported from
 *  it: importing `coreKeybindings` for one number would REGISTER every core binding in the
 *  launcher window too, purely as a side effect of listing chats. */
const ORDINAL_BINDINGS = 9;

/** Keywords every chat row carries. The row's LABEL is already the full title, and both
 *  matchers that see these rows — the DS's `matchCommand` and the seam provider's mirror of
 *  it (`matchesQuery`, usePaletteRegistry.ts) — are substring-over-the-joined-haystack with
 *  the label in it, so the title needs no per-word keywords to be searchable. These carry
 *  only what a title CANNOT say: the words an operator types when they are hunting for a
 *  conversation rather than spelling one ("switch chats", "jump to tab").
 *
 *  Spelled PLURAL on purpose. Both matchers ask `haystack.includes(term)`, so the plural is
 *  the strictly wider spelling — "tabs" answers `tab` AND `tabs`, while "tab" answers only
 *  the singular — and an operator reaching for a LIST of their chats is as likely to type one
 *  as the other. "jump" is the verb the ⌘1–9 bindings already use on themselves ("Jump to
 *  chat tab 3", coreKeybindings.ts), so it is the word Settings ▸ Keyboard has already taught
 *  for this exact move. */
const CHAT_KEYWORDS = [
  "chats", "tabs", "sessions", "conversations", "threads", "switch", "jump", "go to",
];

/** Same list plus the word for the mode, for a memory-free tab (ADR 0069 D3b) — so typing
 *  "incognito" lists exactly those. It rides the keywords rather than the row's one text
 *  slot: the tab strip marks these with an EyeOff, and `hint` here is already spent on
 *  "current" / the ⌘N combo. */
const INCOGNITO_KEYWORDS = [...CHAT_KEYWORDS, "incognito", "private"];

/** The rows for the CURRENT chat-store snapshot, in tab order (left→right, the same order
 *  ⌘1–9 counts in). Exported for tests; it IS the registered source.
 *
 *  No cap. MAX_SESSIONS already bounds this at 50, the mapping is trivial, and any cap
 *  would need an order to cap BY — capping in tab order silently drops the newest tabs
 *  (new sessions append right), and capping by recency is ranking, which belongs to the
 *  root-view work, not here. The empty-query root list is that PR's problem to shorten; a
 *  typed query is exactly when the operator wants every match, capped by nothing. */
export function chatTabPaletteRows(): PaletteCommand[] {
  const { sessions, currentSessionId } = chatStore.getSnapshot();
  return sessions.map((session, i) => {
    // A blank title would render an unreadable empty row. `DEFAULT_SESSION_TITLE` is the
    // honest fallback — it is what the tab strip shows for a chat that has not been named
    // by its first message yet, so the row matches what the operator is looking at. Several
    // unnamed chats therefore share one label, deliberately: the strip says the same thing,
    // the ⌘N hint below tells the first nine apart, and past that an untitled chat is not
    // what someone typing a name is looking for.
    const title = session.title?.trim() || DEFAULT_SESSION_TITLE;
    const isCurrent = session.id === currentSessionId;
    return {
      id: `${CHAT_TAB_ID_PREFIX}${session.id}`,
      label: title,
      group: CHAT_TAB_GROUP,
      keywords: session.incognito ? INCOGNITO_KEYWORDS : CHAT_KEYWORDS,
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
      // the palette row itself works from anywhere. In the launcher window nothing registers
      // the core bindings, so the row shows no combo there — which is the truth there too:
      // ⌘1–9 act on the console window's chat panel.) An explicit `hint` wins over the combo,
      // so the current row shows "current" instead — where the shortcut is moot anyway.
      keybinding: !isCurrent && i < ORDINAL_BINDINGS ? `chat.tab.${i + 1}` : undefined,
      run: (ctx) => {
        // Through the NavIntent chokepoint, NOT `chatStore.switchSession` directly: the
        // frameless desktop launcher mounts this same registry in a shell-less context and
        // forwards intents to the main window, where the store that owns the tabs lives.
        // `setPaletteNavigator` is the ONE seam that swaps that sink (tests included), so
        // this module needs no injection point of its own. The intent is also where the id
        // is re-validated — see `applyNavIntent`.
        navigate({ kind: "chat", sessionId: session.id });
        ctx.close();
      },
    };
  });
}

// Self-register on import, the way core keybindings do (`keybindings/index.ts`). BOTH palette
// hosts pull this module in for the side effect — App (the console window) and Launcher (the
// frameless desktop window), which is why the rows navigate by intent rather than by touching
// the store. A missing side-effect import would fail SILENTLY (no chats in the palette and no
// error anywhere), so chatTabPalette.test.ts guards that every palette host keeps it.
//
// The launcher lists real chats because its store is a SECOND instance hydrated from the same
// `localStorage` key the console window persists to, kept roughly in step by chat-store's
// cross-window `storage` merge — which is what makes a session id meaningful once forwarded
// across the window boundary. "Roughly" is why the id is re-checked on arrival: the two
// windows can key off different agents entirely (see the `chat` arm of `applyNavIntent`).
//
// The HOSTS wire this, never the adapter: `usePaletteRegistry` maps whatever is registered
// onto DS commands and knows nothing about chat tabs — importing a feature module there would
// invert that layering (and would cost the seam's "no source ⇒ no provider wired" assertion
// its last unwired reader, paletteSourceProvider.test.ts).
//
// Registration is unconditional, permanent, and needs no unregister handle: the chat store
// always holds at least one session (`loadPersisted` creates one when storage is empty), so
// there is never a "no rows" state worth withdrawing for, and an HMR re-eval that registers a
// second copy of this function is harmless — its rows lose the registry's id dedup.
registerPaletteSource(chatTabPaletteRows);
