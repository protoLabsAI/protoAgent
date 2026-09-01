// Core's ⌘K rows for the OPEN CHAT TABS (ADR 0061): one row per tab, so the operator
// switches to a chat BY NAME instead of by ordinal.
//
// The gap this closes: ⌘1–9 (`chat.tab.N`, coreKeybindings.ts) jumps to a tab by POSITION,
// and nothing in the console surfaces a session's title outside the tab strip itself — so
// with more than a handful of chats open, "go back to the one about the release notes"
// means reading the strip and counting. The palette is where you type a name.
//
// WHY A BLOCK OF STATICS AND NOT `registerPaletteSource`. These rows have to be live —
// sessions are created and deleted while the console runs, up to MAX_SESSIONS (50) of them,
// the tab strip can be dragged into a new order, and a title is DERIVED from the first user
// message (`titleFromMessages`), so it changes after a chat's first turn. A registration
// made once at import would list the tab the operator just closed, miss the one they just
// opened, and label a titled chat "New chat" forever.
//
// The seam's other half, `registerPaletteSource`, exists for exactly that freshness — but it
// buys it on the PROVIDER path, and that path is the wrong trade for rows that are merely
// observable. A source's premise is "nothing can tell you when the data moved", and here it
// is simply false: `chatStore.subscribe` is that notification. So this module takes it and
// re-registers the rows as a block of STATICS (`registerPaletteCommands`) whenever the tab
// strip actually changes — as live as a source's rows, on the cheaper path.
//
// WHAT THE PROVIDER PATH WOULD STILL COST, on the host-owned root view (#3289,
// `palette/rootView.tsx` — the earlier revision of this comment argued against DS
// `commandsView` defects that view REPLACED, so read it there rather than trusting a
// second-hand account here). Two things survive, and both are contracts rather than bugs:
//
//   • The root view arms `loading` and debounces `getCommands` 120ms for ANY provider that
//     declares it, so a source would put a "Searching…" spinner and a 120ms wait in front of
//     every keystroke in the default console. `hasPaletteSources()` staying false is what
//     keeps ⌘K on the no-provider path.
//   • For that window the PREVIOUS query's provider rows are still listed, because provider
//     rows are ordered and never re-filtered — deliberately, so a remote or fuzzy source's
//     hits are not silently deleted (`rank.ts`, `orderCommands`). A chat title matches no
//     other command, so during the debounce the ranked local corpus is empty and the whole
//     list is stale chat rows — which is what a selection reset to `filtered[0]` then aims
//     Enter at. Type a name, hit Enter without pausing, land in the previous query's chat.
//     Statics are client-filtered per keystroke with nothing retained, so every row on screen
//     matches what was typed. (`e2e/palette-chat-tabs.spec.ts` pins the gesture end to end.)
//
// Neither is a reason for a fork with genuinely unobservable rows to avoid a source; they are
// the reason THESE rows, whose matching is plain substring over a title, do not need one.
//
// Building the rows stays CHEAP and SYNCHRONOUS regardless: one `chatStore.getSnapshot()`
// read and a map over ≤50 sessions, run from a store notification, never from a render.
import { chatStore, DEFAULT_SESSION_TITLE } from "../chat/chat-store";
import { registerPaletteCommands } from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { navigate } from "./usePaletteRegistry";

/** Palette group for the chat rows. Its own group so they read as CHATS — a list of the
 *  operator's conversations — rather than as more commands in the Commands group, and so the
 *  empty-query list owes them a row: `pickRootFill` gives every group a turn before any group
 *  takes a second, which is the only thing standing between fifty open tabs and a root list
 *  made of nothing else. The block is re-inserted after everything registered before it, so on
 *  that list — the one the root view renders headers on (a ranked list has no sections) — they
 *  land as one labelled block at the end. */
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
 *  matcher that sees these rows — `matchCommand` (palette/rank.ts), which the root view and
 *  the seam's provider both go through — is substring-over-the-joined-haystack with the label
 *  in it, so the title needs no per-word keywords to be searchable. These carry only what a
 *  title CANNOT say: the words an operator types when they are hunting for a conversation
 *  rather than spelling one ("switch chats", "jump to tab").
 *
 *  Spelled PLURAL on purpose. The matcher asks `haystack.includes(term)`, so the plural is
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
 *  ⌘1–9 counts in). Pure: it reads the store and returns rows, and `syncChatTabPalette` below
 *  is the only thing that registers them. Exported for tests.
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
        // A row is built when the strip last changed and run whenever the operator reaches
        // for ⌘K, so the id it closes over can be dead by then — `applyNavIntent` re-checks
        // it, because `chatStore.switchSession` does not.
        //
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

// ── Freshness ────────────────────────────────────────────────────────────────────────
// The rows are registered, not read on demand, so something has to notice when the tab strip
// moves. `chatStore.subscribe` fires on EVERY store write — including every streamed token,
// dozens a second — and re-registering on each one would re-run the palette adapter's whole
// command effect at that rate. So the sync is keyed on a signature of what a row is actually
// derived from; everything else the store does (a message landing, a status flipping) leaves
// the palette alone.

/** Everything `chatTabPaletteRows` reads, in the order it reads it: which tab is current (it
 *  decides the "current" hint and whether a ⌘N combo is advertised), then each tab's id,
 *  title and incognito flag, in tab order (position picks the ⌘N, so a drag-reorder has to
 *  count as a change). Fields are joined with control characters no title can contain, so no
 *  title can forge a boundary and make two different strips compare equal. */
function tabSignature(): string {
  const { sessions, currentSessionId } = chatStore.getSnapshot();
  let sig = currentSessionId ?? "";
  for (const s of sessions) {
    sig += `\u0001${s.id}\u0000${s.title?.trim() ?? ""}\u0000${s.incognito ? "1" : ""}`;
  }
  return sig;
}

let _registered: (() => void) | undefined;
let _signature: string | undefined;

/** Re-register the chat rows iff the tab strip actually changed. Idempotent — calling it on
 *  an unchanged store registers nothing and bumps nothing, which is what makes it safe to
 *  hang off a store that notifies on every token. Exported for tests; the module wires it to
 *  the store below. */
export function syncChatTabPalette(): void {
  const signature = tabSignature();
  if (signature === _signature) return;
  _signature = signature;
  // New block FIRST, old handle withdrawn after. The seam's unregister only removes entries
  // it still OWNS, so every surviving row is replaced in place by the new block and the
  // withdrawal deletes exactly the tabs that closed. The other order would leave a reader
  // that ran between the two calls looking at a palette with no chats in it.
  const stale = _registered;
  _registered = registerPaletteCommands(chatTabPaletteRows());
  stale?.();
}

// Register on import and follow the store from there, the way core keybindings self-register
// (`keybindings/index.ts`). BOTH palette hosts pull this module in for the side effect — App
// (the console window) and Launcher (the frameless desktop window), which is why the rows
// navigate by intent rather than by touching the store. A missing side-effect import would
// fail SILENTLY (no chats in the palette and no error anywhere), so chatTabPalette.test.ts
// guards that every palette host keeps it.
//
// The launcher lists real chats because its store is a SECOND instance hydrated from the same
// `localStorage` key the console window persists to, kept roughly in step by chat-store's
// cross-window `storage` merge — which is what makes a session id meaningful once forwarded
// across the window boundary, and which notifies subscribers exactly like a local write, so
// the launcher's rows track the other window's tabs. "Roughly" is why the id is re-checked on
// arrival: the two windows can key off different agents entirely (see the `chat` arm of
// `applyNavIntent`).
//
// The HOSTS wire this, never the adapter: `usePaletteRegistry` maps whatever is registered
// onto DS commands and knows nothing about chat tabs — importing a feature module there would
// invert that layering (and would cost the seam's "no source ⇒ no provider wired" assertion
// its last unwired reader, paletteSourceProvider.test.ts).
//
// Neither the registration nor the subscription is ever withdrawn: the chat store always
// holds at least one session (`loadPersisted` creates one when storage is empty), so there is
// never a "no rows" state worth withdrawing for. An HMR re-eval subscribes a second copy —
// harmless, because both copies register the same ids and the seam's unregister refuses to
// evict an entry it no longer owns, so the newest registration always wins.
syncChatTabPalette();
chatStore.subscribe(syncChatTabPalette);
