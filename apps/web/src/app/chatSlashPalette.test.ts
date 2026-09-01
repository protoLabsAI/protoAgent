// The chat's verbs in ⌘⇧K (#3292). Five things can go wrong here and all five are quiet,
// so all five are pinned:
//
//   1. a row that VISIBLY DOES NOTHING. Most client slash commands `return false` without a
//      session; in the composer that falls through to the draft, from the palette it is a
//      dead row. Every command is bucketed (disabled-with-a-reason vs make-a-thread-first)
//      and both buckets are asserted, including the create → dispatch handoff. `/new` gets
//      its own arm: `createSession` REUSES a pristine blank, so on a blank tab the row is a
//      no-op and must say so.
//   2. a SKILL row that lies. A user-facing skill is a server-side message rewrite applied
//      on the next SEND — there is nothing to run — so its row must prefill the composer and
//      never dispatch.
//   3. rows that VANISH. The gate is "does this WINDOW have the built-in chat", never "is a
//      slot registered right now": the DS AppShell unmounts a collapsed dock, so the latter
//      goes false on a one-click gesture and used to empty the whole Chat + Skills group.
//   4. a MODE row that arms something in a direction its label never named. `/bypass` turns
//      off tool-permission approval; from a fuzzy search, one Enter must never do that.
//   5. a row that is CORRECT and unfindable — or worse, findable UNDER another row. This is a
//      search surface: the operator types words, the palette preselects the first match, and
//      Enter runs it. So the words are pinned as words ("wipe" → /clear), first-match and all.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatStore } from "../chat/chat-store";
import { rankCommands } from "./palette/rank";
import { settingsPaletteCommands } from "./settingsPalette";
import "../chat/coreSlashCommands"; // side-effect: registers the client commands
import { registerSlashDispatcher } from "../chat/slashDispatch";
import type { SlashDispatchTarget } from "../chat/slashDispatch";
import { registeredSlashCommands } from "../ext/slashRegistry";
import type { Command } from "@protolabsai/ui/command-palette";
import type { PaletteCommand } from "../ext/paletteRegistry";
import type { SlashCommand } from "../lib/types";
import { chatPaletteSignature, chatSlashPaletteRows } from "./chatSlashPalette";
import type { NavIntent } from "./palette/nav";
import { matchCommand } from "./palette/rank";

const offs: (() => void)[] = [];
const nav = vi.fn<(intent: NavIntent) => void>();
const dispatch = vi.fn<(raw: string) => boolean>(() => true);
const prefill = vi.fn<(text: string) => void>();

/** Stand in for the visible chat slot's registration (ChatSurface publishes this per render). */
function slot(over: Partial<SlashDispatchTarget> = {}) {
  const off = registerSlashDispatcher({
    run: dispatch,
    sessionId: "sess-1",
    surfaceActive: true,
    prefillDraft: prefill,
    ...over,
  });
  offs.push(off);
}

/** The chat store's answer, which is what the ROWS read — independent of any slot. `over`
 *  patches the current session (`null` sessions = no chat at all). */
function store(over: { session?: Record<string, unknown> | null; blank?: boolean } = {}) {
  const session =
    over.session === null
      ? null
      : {
          id: "sess-1",
          title: over.blank ? "New chat" : "Deploy triage",
          messages: over.blank ? [] : [{ role: "user", content: "hi" }],
          createdAt: 0,
          updatedAt: 0,
          ...(over.session ?? {}),
        };
  vi.spyOn(chatStore, "getSnapshot").mockReturnValue({
    ...chatStore.getSnapshot(),
    sessions: session ? [session] : [],
    currentSessionId: session ? (session.id as string) : null,
  } as ReturnType<typeof chatStore.getSnapshot>);
}

let currentSkills: SlashCommand[] = [];
const skills = (...names: string[]) => {
  currentSkills = names.map((name) => ({
    name,
    description: `The ${name} skill.`,
    kind: "skill",
  }));
};

const rows = (opts: { reachable?: boolean } = {}) =>
  chatSlashPaletteRows(nav, { reachable: opts.reachable ?? true, skills: currentSkills });
const row = (id: string): PaletteCommand => {
  const found = rows().find((r) => r.id === id);
  if (!found) throw new Error(`no palette row ${id} — got ${rows().map((r) => r.id).join(", ")}`);
  return found;
};
/** Run a row the way the DS commands view does (which refuses a disabled one). */
const run = (r: PaletteCommand) => {
  if (r.disabled) return;
  r.run({ close: () => {} });
};

beforeEach(() => {
  nav.mockClear();
  dispatch.mockClear();
  prefill.mockClear();
  currentSkills = [];
  store(); // a live, non-blank chat unless a test says otherwise
});

afterEach(() => {
  while (offs.length) offs.pop()?.();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("which WINDOW gets the rows", () => {
  it("offers NOTHING where no built-in chat exists (the launcher, a fork surface, a plugin slot)", () => {
    // The frameless launcher mounts no ChatSurface at all, and a fork surface or a
    // `slot:"chat"` plugin iframe is resolved BEFORE the built-in one — so nothing here
    // could reach a composer, and a client slash command cannot cross to another window the
    // way a serializable NavIntent can.
    slot();
    expect(rows({ reachable: false })).toEqual([]);
  });

  it("keeps every row while the chat DOCK is collapsed — the seam is unregistered, chat isn't gone", () => {
    // THE REGRESSION THIS FILE EXISTS FOR (blocker, 2026-08): the DS AppShell renders a dock's
    // content only while the dock is open, so "Hide left panel" unmounts ChatSessionSlot and
    // `slashDispatchTarget()` goes null. Gating the rows on that emptied the whole Chat +
    // Skills group out of ⌘⇧K in exactly the state the palette is most useful in — chat out of
    // the way, operator wanting a chat verb without reopening it.
    skills("triage");
    // …no `slot()` call: nothing is registered, exactly as after the collapse.
    const ids = rows().map((r) => r.id);
    expect(ids).toContain("chat-slash:clear");
    expect(ids).toContain("chat-slash:new");
    expect(ids).toContain("chat-skill:triage");
    // …and the rows are LIVE, not a wall of dead ones: the store still has the session.
    expect(row("chat-slash:clear").disabled).toBeFalsy();
  });

  it("RUNS a row picked while the dock is collapsed — raise (which un-collapses), then dispatch", () => {
    vi.useFakeTimers();
    run(row("chat-slash:clear")); // still no slot registered
    // `openView` un-hides the surface AND un-collapses the dock it lives on, so the raise is
    // what brings the slot back; nothing is dispatched into the dark meanwhile.
    expect(nav).toHaveBeenCalledWith({ kind: "view", id: "chat" });
    expect(dispatch).not.toHaveBeenCalled();
    slot(); // React commits, the dock re-renders, the slot re-registers
    vi.advanceTimersByTime(50);
    expect(dispatch).toHaveBeenCalledWith("clear");
  });

  it("does NOT invent a session when the store already has one", () => {
    vi.useFakeTimers();
    const created = vi.spyOn(chatStore, "createSession");
    run(row("chat-slash:help")); // collapsed: no slot to ask
    expect(created).not.toHaveBeenCalled(); // the STORE is the authority, not the seam
  });
});

describe("client slash command rows", () => {
  it("labels a row `/token · what it does` — the composer `/` menu's own shape", () => {
    slot();
    const ids = rows().map((r) => r.id);
    for (const cmd of registeredSlashCommands()) expect(ids).toContain(`chat-slash:${cmd.name}`);
    expect(row("chat-slash:clear").label).toBe("/clear · Clear this chat's history");
    expect(row("chat-slash:clear").group).toBe("Chat");
  });

  it("still says what the row DOES when the hint is spent on a reason", () => {
    // The regression this guards: put the description in the hint and it vanishes in exactly
    // the state an operator opens the palette in — no chat yet, so every hint is a caveat.
    store({ session: null });
    const clear = row("chat-slash:clear");
    expect(clear.hint).toBe("needs an open chat"); // the hint slot is the REASON here…
    expect(clear.label).toContain("Clear this chat's history"); // …and the label still explains
    expect(clear.keywords?.join(" ")).toContain("wipe"); // plus reach-for-it synonyms
  });

  it("searches the ARGUMENT words too — they are what an operator types", () => {
    // `usage` is the only field carrying "on|off", "low|medium|high"; a row found by
    // "incognito off" is a row the operator can pick without knowing the syntax first.
    expect(row("chat-slash:incognito").keywords?.join(" ")).toContain("off");
    expect(row("chat-slash:effort").keywords?.join(" ")).toContain("max");
  });

  it("carries /publish's flag onto the row instead of evaluating it", () => {
    // Evaluating a flag here would run during the fail-closed window while /api/flags is in
    // flight (ADR 0068) and hide the row for the life of the window. The host resolves it
    // per render, so a late answer reveals it.
    expect(row("chat-slash:publish").flag).toBe("chat.publish");
    expect(row("chat-slash:new").flag).toBeUndefined();
  });

  it("advertises a keybinding by ID, and leaves the hint free so the LIVE combo renders", () => {
    expect(row("chat-slash:new").keybinding).toBe("chat.new");
    expect(row("chat-slash:new").hint).toBeUndefined();
    expect(row("chat-slash:clear").keybinding).toBe("chat.clear");
    expect(row("chat-slash:clear").hint).toBeUndefined();
    // Never a literal combo anywhere — that is exactly what `keybinding` prevents.
    for (const r of rows()) expect(r.hint ?? "").not.toMatch(/[⌘⇧⌥⌃]/);
  });
});

describe("a per-tab MODE is never a one-Enter row", () => {
  // `/bypass` arms `run_command` auto-approval; `/incognito` decides whether the turn is
  // remembered. Dispatched bare, both TOGGLE — so a fuzzy-matched palette row that dispatched
  // one would flip a trust boundary in a direction its label never named. Both DRAFT instead,
  // and both state the mode's current value, which is the thing worth opening ⌘⇧K for.
  it("DRAFTS rather than dispatching, so nothing in the palette can arm auto-approval", () => {
    slot();
    for (const name of ["bypass", "incognito"]) {
      const r = row(`chat-slash:${name}`);
      expect(r.hint).toBe("drafts in chat — you send it");
      run(r);
      expect(prefill).toHaveBeenCalledWith(`/${name} `);
    }
    expect(dispatch).not.toHaveBeenCalled(); // …not once, in either direction
  });

  it("states the CURRENT value in the label, in both directions", () => {
    store({ session: { bypassPermissions: true, incognito: false } });
    expect(row("chat-slash:bypass").label).toContain("— now on");
    expect(row("chat-slash:incognito").label).toContain("— now off");
    store({ session: { bypassPermissions: false, incognito: true } });
    expect(row("chat-slash:bypass").label).toContain("— now off");
    expect(row("chat-slash:incognito").label).toContain("— now on");
  });

  it("needs a real tab: a mode has no current value without one", () => {
    store({ session: null });
    for (const name of ["bypass", "incognito"]) {
      const r = row(`chat-slash:${name}`);
      expect(r.disabled).toBe(true);
      expect(r.hint).toBe("needs an open chat");
      expect(r.label).not.toContain("now"); // …and doesn't claim a state it can't read
    }
  });
});

describe("session semantics — the decision, per command", () => {
  const THIS_CHAT = [
    "clear", "export", "publish", "btw", "trajectory", "prompt", "perf", "compact",
    "bypass", "incognito",
  ];
  const THREAD_ONLY = ["help", "effort", "model", "goal", "watch"];

  it("disables the commands that need THIS chat, and says why", () => {
    store({ session: null });
    for (const name of THIS_CHAT) {
      const r = row(`chat-slash:${name}`);
      expect(r.disabled, `/${name} must not look runnable with no chat`).toBe(true);
      // The reason OUTRANKS every other hint, /btw's draft promise included: a dead row that
      // doesn't say why is worse than no row at all.
      expect(r.hint).toBe("needs an open chat"); // still LISTED, and explains itself
    }
  });

  it("keeps the commands that need only A thread runnable, promising the tab in the hint", () => {
    store({ session: null });
    for (const name of THREAD_ONLY) {
      const r = row(`chat-slash:${name}`);
      expect(r.disabled, `/${name} needs a thread, not this one — it can make one`).toBeFalsy();
      expect(r.hint).toBe("opens a chat first");
    }
    expect(row("chat-slash:new").disabled).toBeFalsy(); // needs nothing at all
    expect(row("chat-slash:new").hint).toBeUndefined(); // …and promises no tab it isn't making
  });

  it("disables /new on a blank tab, where createSession hands the SAME tab back", () => {
    // The store deliberately reuses a pristine blank rather than piling empties up, so on a
    // blank tab `/new` is a genuine no-op — which every other "new chat" affordance
    // (MobileShell, SessionSheet) already disables itself for. The palette row was the one
    // that didn't, and it fails in the state a fresh console BOOTS into.
    store({ blank: true });
    expect(row("chat-slash:new").disabled).toBe(true);
    expect(row("chat-slash:new").hint).toBe("already on a blank chat");
    // A blank on ANOTHER tab still switches you there — visible feedback, so it stays live.
    store();
    expect(row("chat-slash:new").disabled).toBeFalsy();
  });

  it("re-enables every row the moment a session exists — the state is LIVE, never snapshotted", () => {
    store({ session: null });
    expect(rows().filter((r) => r.disabled)).toHaveLength(THIS_CHAT.length);
    store();
    expect(rows().filter((r) => r.disabled)).toEqual([]);
  });

  it("accounts for EVERY registered command — no row falls between the buckets", () => {
    expect(new Set([...THIS_CHAT, ...THREAD_ONLY, "new"])).toEqual(
      new Set(registeredSlashCommands().map((c) => c.name)),
    );
  });
});

describe("running a row", () => {
  it("dispatches straight through when the operator is already on a live chat", () => {
    slot();
    run(row("chat-slash:effort"));
    expect(dispatch).toHaveBeenCalledWith("effort");
    expect(nav).not.toHaveBeenCalled(); // already there — don't yank the view around
  });

  it("dispatches `/goal new` — the only branch the CLIENT command claims", () => {
    // Bare `/goal` returns false (it falls through to the SERVER control command), so a row
    // that dispatched "goal" would be the exact silent no-op this module exists to avoid.
    slot();
    // …and the label can't over-promise: the registry description LEADS with the two
    // branches (`/goal <text>`, `/goal clear`) this row does NOT run, so the row restates it.
    expect(row("chat-slash:goal").label).toBe("/goal new · Open the guided goal form");
    run(row("chat-slash:goal"));
    expect(dispatch).toHaveBeenCalledWith("goal new");
  });

  it("DRAFTS /btw instead of running it — the command needs a question we can't ask for", () => {
    // Dispatched bare, /btw posts its own "ask a side question after /btw" note and stops.
    // Prefilling lands the operator in the composer with the token typed, as the `/` menu does.
    slot();
    expect(row("chat-slash:btw").hint).toBe("drafts in chat — you send it");
    run(row("chat-slash:btw"));
    expect(prefill).toHaveBeenCalledWith("/btw ");
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("raises the chat surface first when it is hidden, through the NavIntent chokepoint", () => {
    // A direct useUI.getState() call is an inert no-op in the frameless launcher's
    // shell-less context — every navigation has to be a serializable intent.
    vi.useFakeTimers();
    slot({ surfaceActive: false });
    run(row("chat-slash:help"));
    expect(nav).toHaveBeenCalledWith({ kind: "view", id: "chat" });
    // /help answers through a system note; dispatching before the surface is on screen
    // would draw it into a `display: none` subtree.
    expect(dispatch).not.toHaveBeenCalled();
    vi.advanceTimersByTime(50);
    expect(dispatch).toHaveBeenCalledWith("help");
  });

  it("creates a thread, WAITS for the new slot, then dispatches", () => {
    vi.useFakeTimers();
    store({ session: null });
    slot({ sessionId: null });
    // The store write only lands on React's next commit, so the seam still reports the old
    // session-less target until the new slot re-registers — model exactly that.
    const created = vi.spyOn(chatStore, "createSession").mockImplementation(() => {
      slot({ sessionId: "sess-new" });
      return { id: "sess-new" } as ReturnType<typeof chatStore.createSession>;
    });

    run(row("chat-slash:model"));
    expect(created).toHaveBeenCalled();
    expect(dispatch).not.toHaveBeenCalled(); // not into the dark
    vi.advanceTimersByTime(50);
    expect(dispatch).toHaveBeenCalledWith("model");
  });

  it("gives up rather than looping when a slot never produces a session", () => {
    vi.useFakeTimers();
    store({ session: null });
    slot({ sessionId: null });
    vi.spyOn(chatStore, "createSession").mockImplementation(
      () => ({ id: "x" }) as ReturnType<typeof chatStore.createSession>,
    );
    run(row("chat-slash:watch"));
    vi.advanceTimersByTime(60_000);
    expect(dispatch).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0); // bounded — no timer left spinning
  });
});

describe("what an operator types finds the row they mean", () => {
  // The host's REAL inclusion filter (`palette/rank.ts`, itself a verbatim port of the DS
  // matcher), not a restatement of it — a mirror here would be a third copy free to drift
  // from the two that decide what the operator actually sees. `rank.ts` imports only a type
  // from the DS, so this costs nothing.
  //
  // What is pinned is the ORDER it produces, in registration order — which is what the
  // untyped list renders, and the pool the typed list ranks. A keyword that lets one command
  // outrank another's own name is the "typing /model runs /trajectory" trap coreSlashCommands
  // warns about, one surface over: `/goal`'s usage string legitimately contains "clear".
  const first = (query: string) =>
    rows().find((r) => matchCommand(r as unknown as Command, query))?.id;

  beforeEach(() => {
    slot();
    skills("triage");
  });

  it.each([
    ["shortcuts", "settings:keybindings"],
    ["rebind", "settings:keybindings"],
    ["dark mode", "settings:theme"],
    ["rag", "settings:knowledge"],
    ["backup", "settings:snapshot"],
  ])("Settings still wins %j in the MERGED palette (#3292 regression)", (q, expected) => {
    // THE GAP THIS CLOSES. settingsPalette.test.ts pins these queries against settings rows
    // ALONE, with a local substring helper — so it cannot see what happens once the chat
    // source is merged in and the REAL ranker orders both. #3292 gave /help the words
    // "shortcuts" and "keys", already owned by Settings' Keyboard row (#3291); /help won the
    // merged ranking, typing "shortcuts" stopped opening the Keyboard pane, and the only
    // thing that noticed was settings-palette.spec.ts — an E2E, on branches that had touched
    // no console code at all.
    //
    // Cross-source keyword OVERLAP is fine and common here (~19 pairs: "cost", "llm",
    // "trace"…); the vocabularies are meant to overlap and the ranker arbitrates. What must
    // not change silently is which row WINS. So this pins winners, not disjointness.
    slot();
    skills("triage");
    const merged = [...settingsPaletteCommands(() => {}), ...chatSlashPaletteRows(nav, { reachable: true })];
    expect(rankCommands(merged, q)[0]?.id).toBe(expected);
  });

  it.each([
    // The command's own name never loses to a neighbour that merely mentions it.
    ["clear", "chat-slash:clear"], // …though `/goal`'s usage reads "/goal new · /goal <text> · /goal clear"
    ["model", "chat-slash:model"],
    ["prompt", "chat-slash:prompt"],
    // The words you reach for when you DON'T know the token — the reason a synonym list exists.
    ["wipe", "chat-slash:clear"],
    ["start over", "chat-slash:clear"],
    ["download transcript", "chat-slash:export"],
    ["share link", "chat-slash:publish"],
    ["what the agent saw", "chat-slash:trajectory"],
    ["system prompt", "chat-slash:prompt"],
    ["latency", "chat-slash:perf"],
    ["context window", "chat-slash:compact"],
    ["thinking", "chat-slash:effort"],
    ["llm", "chat-slash:model"],
    ["forget", "chat-slash:incognito"],
    ["keyboard shortcuts", "chat-slash:help"],
    ["yolo", "chat-slash:bypass"],
    ["notify", "chat-slash:watch"],
    // The argument words, which live only in `usage` — a row you can pick without knowing
    // the syntax first.
    ["incognito off", "chat-slash:incognito"],
    ["effort max", "chat-slash:effort"],
    // A skill is findable by what it DOES, not only by whatever its author named it.
    ["triage skill", "chat-skill:triage"],
  ])("typing %j lands on %s", (query, id) => {
    expect(first(query)).toBe(id);
  });

  it("cannot land Enter on a row that ARMS anything, however it is spelled", () => {
    // The trust-boundary arm. Whatever the fuzzy match resolves to for the words an operator
    // reaches for when they mean bypass, the row it preselects must be a DRAFTING one — the
    // direction and the send stay theirs.
    for (const q of ["yolo", "bypass", "bypass on", "auto-approve", "dangerous", "run_command"]) {
      const id = first(q);
      const r = id ? rows().find((x) => x.id === id) : undefined;
      if (!r) continue;
      run(r);
    }
    expect(dispatch).not.toHaveBeenCalledWith(expect.stringContaining("bypass"));
  });
});

describe("user-facing skill rows", () => {
  it("PREFILLS the composer and never dispatches — a skill is a send-time rewrite", () => {
    slot();
    skills("triage");
    const r = row("chat-skill:triage");
    // A skill's token is whatever its author called it, so the description is the only thing
    // on the row that names what it does — it must be ON the row, not only in `keywords`.
    expect(r.label).toBe("/triage · The triage skill.");
    expect(r.group).toBe("Skills");
    expect(r.hint).toBe("drafts in chat — you send it"); // the row promises a draft, not a run
    run(r);
    expect(prefill).toHaveBeenCalledWith("/triage "); // trailing space = the caret affordance
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("opens a chat first when there is no thread to type into", () => {
    vi.useFakeTimers();
    store({ session: null });
    slot({ sessionId: null });
    skills("triage");
    vi.spyOn(chatStore, "createSession").mockImplementation(() => {
      slot({ sessionId: "sess-new" });
      return { id: "sess-new" } as ReturnType<typeof chatStore.createSession>;
    });
    run(row("chat-skill:triage"));
    expect(prefill).not.toHaveBeenCalled();
    vi.advanceTimersByTime(50);
    expect(prefill).toHaveBeenCalledWith("/triage ");
  });

  it("lists only skills — a workflow or subagent runs server-side and is a different row", () => {
    slot();
    currentSkills = [
      { name: "triage", description: "A skill.", kind: "skill" },
      { name: "research-and-brief", description: "A workflow.", kind: "workflow" },
      { name: "dream", description: "A subagent.", kind: "subagent" },
      { name: "lifecycle", description: "A control command.", kind: "control" },
    ];
    const ids = rows().map((r) => r.id);
    expect(ids).toContain("chat-skill:triage");
    expect(ids.filter((id) => id.startsWith("chat-skill:"))).toEqual(["chat-skill:triage"]);
  });

  it("drops a skill a CLIENT command already owns — the composer dedups client-first", () => {
    slot();
    skills("help", "triage");
    // Typing /help hits the client command, so a skill row for it would offer a token
    // something else intercepts.
    expect(rows().map((r) => r.id)).not.toContain("chat-skill:help");
    expect(rows().map((r) => r.id)).toContain("chat-skill:triage");
  });
});

describe("the signature the host re-registers on", () => {
  // The rows are STATIC (see the module header: a DS provider keeps the PREVIOUS query's rows
  // listed and runnable through its 120ms debounce, which is not a thing to do with rows that
  // run `/clear`). So something has to re-take the snapshot, and that something is this
  // string. It must move when a row would look different — and stay put otherwise, because
  // the chat store notifies on every streamed token and re-registering the whole group per
  // frame is a real cost.
  it("moves when the current session changes", () => {
    store();
    const before = chatPaletteSignature();
    store({ session: { id: "sess-2" } });
    expect(chatPaletteSignature()).not.toBe(before);
  });

  it("moves when a per-tab MODE flips — the label states it, so the row must be re-taken", () => {
    store({ session: { bypassPermissions: false } });
    const before = chatPaletteSignature();
    store({ session: { bypassPermissions: true } });
    expect(chatPaletteSignature()).not.toBe(before);
  });

  it("moves when the current tab becomes the reusable blank — /new's disabled state", () => {
    store();
    const before = chatPaletteSignature();
    store({ blank: true });
    expect(chatPaletteSignature()).not.toBe(before);
  });

  it("stays PUT for a change no row shows — a streamed token must not churn the palette", () => {
    store({ session: { messages: [{ role: "user", content: "hi" }] } });
    const before = chatPaletteSignature();
    store({ session: { messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "the" }] } });
    expect(chatPaletteSignature()).toBe(before);
  });
});
