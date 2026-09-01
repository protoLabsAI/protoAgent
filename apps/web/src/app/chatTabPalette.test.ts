// The chat-tab palette source (ADR 0061's headline dynamic use case): a row per OPEN CHAT
// TAB, so a chat is reachable by NAME rather than only by the ⌘1–9 ordinal.
//
// Three things carry the weight here. The rows must track the LIVE store — a snapshot would
// list a closed tab and miss a new one. Running a row must never be able to leave the chat
// surface pointing at a session that no longer exists: `chatStore.switchSession` does not
// validate its argument, so a stale id sets `currentSessionId` to nothing, pushes a phantom
// into `activeSessions`, and ChatSurface's `useSession` mounts a DEAD SLOT — and rows are
// built when the palette is read and run a keystroke later, so that window is real. And the
// rows must be FINDABLE by what an operator actually types, which is asserted through the
// provider the palette reads (`paletteSourceProvider`) rather than against a keywords array:
// a correct row nobody can search for is a feature nobody has.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatStore, DEFAULT_SESSION_TITLE } from "../chat/chat-store";
import { registeredPaletteCommands } from "../ext/paletteRegistry";
import { registeredKeybindings } from "../ext/keybindingRegistry";
import "../keybindings/coreKeybindings"; // registers ⌘1–9 (side effect), exactly as App does
import {
  CHAT_TAB_GROUP,
  CHAT_TAB_ID_PREFIX,
  chatTabPaletteRows,
} from "./chatTabPalette"; // importing also self-registers the source (side effect)
import { applyNavIntent, paletteSourceProvider, setPaletteNavigator } from "./usePaletteRegistry";
import { useUI } from "../state/uiStore";
import type { PaletteCommand } from "../ext/paletteRegistry";

const ctx = { close: vi.fn() };

/** Every source row the palette would show for `query`, by label — the same call the DS
 *  commands view makes on open and on each keystroke (it hands `getCommands` an AbortSignal
 *  the synchronous seam provider ignores). Only this file's source is registered, so what
 *  comes back is the chat rows and nothing else. */
function search(query: string): string[] {
  const rows =
    paletteSourceProvider(
      () => true,
      true,
    ).getCommands?.(query, { signal: new AbortController().signal }) ?? [];
  return (rows as { label: string }[]).map((c) => c.label);
}

/** One fresh, single-session store, so each test starts from a known tab strip. */
function resetSessions(): string {
  for (const s of chatStore.getSnapshot().sessions) chatStore.deleteSession(s.id);
  return chatStore.createSession().id;
}

/** Open a named tab (the title is what a first user message would derive). Note
 *  `createSession` hands back a pristine blank tab rather than piling up duplicates, so the
 *  FIRST call after a reset renames the placeholder in place instead of appending. */
function openTab(title: string): string {
  const id = chatStore.createSession().id;
  chatStore.renameSession(id, title);
  return id;
}

const rowFor = (rows: PaletteCommand[], id: string) =>
  rows.find((r) => r.id === `${CHAT_TAB_ID_PREFIX}${id}`);

beforeEach(() => {
  resetSessions();
  ctx.close.mockClear();
});

afterEach(() => {
  setPaletteNavigator(null);
});

describe("chat-tab palette rows", () => {
  it("lists one row per open tab, titled and grouped", () => {
    const a = openTab("Release notes for v0.156");
    const b = openTab("Fleet smoke test");

    const rows = chatTabPaletteRows();
    const ra = rowFor(rows, a)!;
    expect(ra.label).toBe("Release notes for v0.156");
    // Their own group, so they read as CHATS rather than as more commands.
    expect(ra.group).toBe(CHAT_TAB_GROUP);
    expect(rowFor(rows, b)!.label).toBe("Fleet smoke test");
    // Tab order (left→right), the same order ⌘1–9 counts in.
    expect(rows.map((r) => r.label).slice(-2)).toEqual([
      "Release notes for v0.156",
      "Fleet smoke test",
    ]);
  });

  it("labels an untitled chat honestly instead of rendering an empty row", () => {
    // A blank title can only arrive from a persisted/recovered payload — `renameSession`
    // already normalizes one away — which is exactly the case the fallback exists for.
    chatStore.hydrateSessions([
      {
        id: "recovered-blank",
        title: "   ",
        messages: [{ role: "user", content: "hi" }],
        createdAt: 1,
        updatedAt: 2,
      },
    ]);
    const row = rowFor(chatTabPaletteRows(), "recovered-blank");
    expect(row?.label).toBe(DEFAULT_SESSION_TITLE);
    expect(row?.label.trim()).not.toBe("");
  });

  it("marks the current chat with a hint and keeps it runnable", () => {
    const other = openTab("Other");
    const current = openTab("Current");
    expect(chatStore.getSnapshot().currentSessionId).toBe(current);

    const rows = chatTabPaletteRows();
    expect(rowFor(rows, current)!.hint).toBe("current");
    // NOT disabled: from another surface the row is a real navigation back to chat, so
    // disabling it would kill the action exactly where it earns its place.
    expect(rowFor(rows, current)!.disabled).toBeUndefined();
    expect(rowFor(rows, other)!.hint).toBeUndefined();
  });

  it("advertises the ⌘1–9 binding by ID for the first nine tabs, never a literal combo", () => {
    const first = openTab("First");
    const second = openTab("Second");
    chatStore.switchSession(first); // so `second` isn't the current row (a hint would win)

    const rows = chatTabPaletteRows();
    // A binding id: the host renders the LIVE combo, so the row can't lie after a rebind.
    expect(rowFor(rows, second)!.keybinding).toBe("chat.tab.2");
    expect(rowFor(rows, second)!.hint).toBeUndefined();

    // Only the nine positions that HAVE a binding advertise one.
    for (let i = 0; i < 9; i++) openTab(`Tab ${i}`);
    const many = chatTabPaletteRows();
    expect(many.length).toBeGreaterThan(9);
    expect(many[8].keybinding).toBe("chat.tab.9");
    expect(many[9].keybinding).toBeUndefined();
  });

  it("advertises binding ids that REALLY exist, and every ordinal binding there is", () => {
    // The row names a binding by id and the host silently renders no combo when the lookup
    // misses, so a typo (`chat.tabs.1`) would ship as a row that just never shows a shortcut.
    // Pinned in both directions against the registry `coreKeybindings` filled: if that file's
    // ⌘1–9 loop ever grows a tenth jump, "the first nine" stops being the whole story here.
    for (let i = 0; i < 12; i++) openTab(`Tab ${i}`);
    const advertised = chatTabPaletteRows()
      .map((r) => r.keybinding)
      .filter((id): id is string => !!id);
    const ordinals = registeredKeybindings()
      .map((b) => b.id)
      .filter((id) => /^chat\.tab\.\d+$/.test(id));

    expect(ordinals.length).toBeGreaterThan(0); // the import above really did register them
    expect([...advertised].sort()).toEqual([...ordinals].sort());
  });

  it("re-reads the live store on every call — a closed tab stops being listed", () => {
    const gone = openTab("Scratch");
    expect(rowFor(chatTabPaletteRows(), gone)).toBeTruthy();

    chatStore.deleteSession(gone);
    // Nothing bumped the seam's version and nothing re-rendered: the rows are recomputed
    // because they are recomputed, which is the whole reason this is a source.
    expect(rowFor(chatTabPaletteRows(), gone)).toBeUndefined();

    const renamed = openTab("Before");
    chatStore.renameSession(renamed, "After");
    expect(rowFor(chatTabPaletteRows(), renamed)!.label).toBe("After");
  });

  it("is registered as a palette SOURCE, so the palette re-reads it per keystroke", () => {
    const titled = openTab("Registered through the seam");
    const dynamic = registeredPaletteCommands("dynamic").map((c) => c.id);
    expect(dynamic).toContain(`${CHAT_TAB_ID_PREFIX}${titled}`);
    // Never snapshotted into the static half — a frozen copy there wins the dedup and
    // shadows the live row forever.
    expect(registeredPaletteCommands("static").map((c) => c.id)).not.toContain(
      `${CHAT_TAB_ID_PREFIX}${titled}`,
    );
  });
});

describe("finding a chat by typing", () => {
  // The rows go through the palette's real search here, not a keywords assertion: every term
  // has to land somewhere in label/hint/group/keywords, so this is what actually decides
  // whether the words we chose reach the chat the operator meant.
  it("finds a chat by any word of its title", () => {
    openTab("Release notes for v0.156");
    openTab("Fleet smoke test");

    expect(search("release")).toEqual(["Release notes for v0.156"]);
    expect(search("v0.156")).toEqual(["Release notes for v0.156"]); // punctuation and all
    expect(search("SMOKE")).toEqual(["Fleet smoke test"]); // case-insensitive
    // Terms may span fields and arrive in any order — the title plus what the row IS.
    expect(search("smoke chat")).toEqual(["Fleet smoke test"]);
  });

  it("lists every chat for the words an operator types when hunting one", () => {
    openTab("Release notes for v0.156");
    openTab("Fleet smoke test");
    // Said out loud: "switch chats", "jump to tab", "that other session/conversation/thread".
    // BOTH spellings of every noun. The keywords are stored PLURAL because the match is a
    // substring of the joined haystack, which makes the plural the one spelling that answers
    // the singular query too — asserted rather than trusted, since a well-meaning edit back to
    // the singular reads harmless and silently loses everyone who types "tabs".
    const nouns = ["chat", "tab", "session", "conversation", "thread"];
    for (const q of [...nouns, ...nouns.map((n) => `${n}s`), "switch", "jump", "go to"]) {
      expect(search(q), q).toHaveLength(2);
    }
  });

  it("finds the memory-free tabs by the name of the mode", () => {
    // Incognito (ADR 0069 D3b) has no room in the row's one text slot — the ⌘N combo and
    // "current" own it — so the keywords are how you pick those tabs out of the list.
    const secret = chatStore.createSession({ incognito: true }).id;
    chatStore.renameSession(secret, "Comp review");
    openTab("Release notes");

    expect(search("incognito")).toEqual(["Comp review"]);
    expect(search("private")).toEqual(["Comp review"]);
    expect(search("chat")).toHaveLength(2); // still an ordinary chat row otherwise
  });

  it("returns nothing rather than everything when no chat matches", () => {
    openTab("Release notes");
    expect(search("kubernetes")).toEqual([]);
    expect(search("release notes kubernetes")).toEqual([]); // EVERY term must hit
  });
});

describe("running a chat-tab row", () => {
  it("switches the tab and routes to the chat surface", () => {
    const target = openTab("Target");
    const later = openTab("Later");
    expect(chatStore.getSnapshot().currentSessionId).toBe(later);
    useUI.getState().setSurface("knowledge");

    rowFor(chatTabPaletteRows(), target)!.run(ctx);

    expect(chatStore.getSnapshot().currentSessionId).toBe(target);
    expect(chatStore.getSnapshot().activeSessions).toContain(target);
    // A chat you can't see isn't a switch: the row navigates to the chat surface too.
    expect(useUI.getState().surface).toBe("chat");
    expect(ctx.close).toHaveBeenCalled();
  });

  it("routes through the NavIntent chokepoint, not the store directly", () => {
    // The frameless desktop launcher mounts this same registry in a shell-less context and
    // swaps the sink to forward intents to the main window. A row that called
    // `chatStore.switchSession` itself would be a silent no-op there.
    const target = openTab("Forwarded");
    const other = openTab("Stay here");
    const sink = vi.fn();
    setPaletteNavigator(sink);

    rowFor(chatTabPaletteRows(), target)!.run(ctx);

    expect(sink).toHaveBeenCalledWith({ kind: "chat", sessionId: target });
    // Nothing local moved — the intent went to the sink, which is the point.
    expect(chatStore.getSnapshot().currentSessionId).toBe(other);
  });

  it("no-ops the switch when the session died between the read and the run", () => {
    // THE hazard: a source's rows are built at query time and run a keystroke later. In
    // that window another browser tab's cross-tab merge (or a background job) can delete
    // the session. `switchSession` does not validate, so an unguarded run would point
    // `currentSessionId` at nothing and ChatSurface would mount a dead slot.
    const survivor = openTab("Survivor");
    const doomed = openTab("Doomed");
    const row = rowFor(chatTabPaletteRows(), doomed)!; // read…
    chatStore.switchSession(survivor);
    chatStore.deleteSession(doomed); // …deleted before the operator hits Enter

    row.run(ctx);

    const after = chatStore.getSnapshot();
    expect(after.currentSessionId).toBe(survivor); // not the dead id
    // No dead slot: whatever the tab strip points at exists, and no phantom got parked in
    // the active set (where it would have EVICTED a real tab at MAX_ACTIVE_SESSIONS).
    expect(after.sessions.some((s) => s.id === after.currentSessionId)).toBe(true);
    expect(after.activeSessions.every((id) => after.sessions.some((s) => s.id === id))).toBe(true);
    // The operator still lands on chat, where the missing tab is visibly gone — better
    // feedback than a keypress that does nothing at all.
    expect(useUI.getState().surface).toBe("chat");
  });

  it("guards the intent itself, because the run() side can't see the right store", () => {
    // The check lives in `applyNavIntent`, not in the row's `run()`: in the launcher, run()
    // executes in a different JS context from the store that is about to be mutated.
    const survivor = chatStore.getSnapshot().sessions[0].id;
    applyNavIntent({ kind: "chat", sessionId: "chat-never-existed" });
    expect(chatStore.getSnapshot().currentSessionId).toBe(survivor);

    // And this is what the guard prevents — the store does NOT check the id itself.
    // (Self-repairing: deleting the phantom id drops it from `activeSessions` and re-points
    // `currentSessionId` at a real session, so the store is clean for the next test.)
    chatStore.switchSession("chat-never-existed");
    const broken = chatStore.getSnapshot();
    expect(broken.sessions.some((s) => s.id === broken.currentSessionId)).toBe(false);
    expect(broken.activeSessions).toContain("chat-never-existed");
    chatStore.deleteSession("chat-never-existed");
    expect(chatStore.getSnapshot().activeSessions).not.toContain("chat-never-existed");
  });
});

describe("the palette hosts wire the source", () => {
  // A palette host that forgets `import "./chatTabPalette"` fails SILENTLY: the palette simply
  // has no chats in it, with no error anywhere. The hosts own that wiring (the adapter
  // deliberately doesn't import the module — see the note at the foot of chatTabPalette.ts),
  // so the guard belongs on the hosts. Vite `?raw` globs rather than node:fs — same reason as
  // statusTokenGuard.test.ts: this tsconfig has no node types, and under jsdom
  // `import.meta.url` is an http: URL, so URL-relative filesystem access is a trap.
  const APP_SOURCES = import.meta.glob("./*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;

  it("every palette host imports it for the side effect", () => {
    const hosts = Object.entries(APP_SOURCES).filter(([, src]) => /usePaletteRegistry\(/.test(src));
    // Pinned, so the glob can't silently go empty — and so a THIRD palette host has to answer
    // this question rather than quietly shipping a palette with no chats in it.
    expect(hosts.map(([file]) => file).sort()).toEqual(["./App.tsx", "./Launcher.tsx"]);
    for (const [file, src] of hosts) {
      expect(src, file).toContain('import "./chatTabPalette"');
    }
  });
});
