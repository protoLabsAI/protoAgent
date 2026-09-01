// The chat's verbs in ⌘⇧K (#3292). Three things can go wrong here and all three are quiet,
// so all three are pinned:
//
//   1. a row that VISIBLY DOES NOTHING. Most client slash commands `return false` without a
//      session; in the composer that falls through to the draft, from the palette it is a
//      dead row. Every command is bucketed (disabled-with-a-reason vs make-a-thread-first)
//      and both buckets are asserted, including the create → dispatch handoff.
//   2. a SKILL row that lies. A user-facing skill is a server-side message rewrite applied
//      on the next SEND — there is nothing to run — so its row must prefill the composer and
//      never dispatch.
//   3. a source that is EXPENSIVE. It runs on every keystroke into the palette, so it must
//      read the React-Query cache rather than fetch, and track it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatStore } from "../chat/chat-store";
import "../chat/coreSlashCommands"; // side-effect: registers the 16 client commands
import { registerSlashDispatcher } from "../chat/slashDispatch";
import type { SlashDispatchTarget } from "../chat/slashDispatch";
import { registeredPaletteCommands } from "../ext/paletteRegistry";
import { registeredSlashCommands } from "../ext/slashRegistry";
import { queryKeys } from "../lib/queries";
import { queryClient } from "../lib/queryClient";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { chatSlashPaletteRows, registerChatSlashPalette } from "./chatSlashPalette";
import type { NavIntent } from "./usePaletteRegistry";

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

const rows = () => chatSlashPaletteRows(nav);
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

const skills = (...names: string[]) =>
  queryClient.setQueryData(queryKeys.chatCommands, {
    commands: names.map((name) => ({ name, description: `The ${name} skill.`, kind: "skill" })),
  });

beforeEach(() => {
  nav.mockClear();
  dispatch.mockClear();
  prefill.mockClear();
});

afterEach(() => {
  while (offs.length) offs.pop()?.();
  queryClient.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("no chat slot in this window", () => {
  it("offers NOTHING rather than a wall of dead rows (the desktop launcher)", () => {
    // The frameless launcher mounts no ChatSurface at all, so nothing here could reach a
    // composer — and a client slash command cannot cross to the main window the way a
    // serializable NavIntent can.
    expect(rows()).toEqual([]);
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
    slot({ sessionId: null });
    const clear = row("chat-slash:clear");
    expect(clear.hint).toBe("needs an open chat"); // the hint slot is the REASON here…
    expect(clear.label).toContain("Clear this chat's history"); // …and the label still explains
    expect(clear.keywords?.join(" ")).toContain("wipe"); // plus reach-for-it synonyms
  });

  it("searches the ARGUMENT words too — they are what an operator types", () => {
    slot();
    // `usage` is the only field carrying "on|off", "low|medium|high"; a row found by
    // "incognito off" is a row the operator can pick without knowing the syntax first.
    expect(row("chat-slash:incognito").keywords?.join(" ")).toContain("off");
    expect(row("chat-slash:effort").keywords?.join(" ")).toContain("max");
  });

  it("carries /publish's flag onto the row instead of evaluating it", () => {
    // Evaluating a flag here would run during the fail-closed window while /api/flags is in
    // flight (ADR 0068) and hide the row for the life of the window. The host resolves it
    // per render, so a late answer reveals it.
    slot();
    expect(row("chat-slash:publish").flag).toBe("chat.publish");
    expect(row("chat-slash:new").flag).toBeUndefined();
  });

  it("advertises a keybinding by ID, and leaves the hint free so the LIVE combo renders", () => {
    slot();
    expect(row("chat-slash:new").keybinding).toBe("chat.new");
    expect(row("chat-slash:new").hint).toBeUndefined();
    expect(row("chat-slash:clear").keybinding).toBe("chat.clear");
    expect(row("chat-slash:clear").hint).toBeUndefined();
    // Never a literal combo anywhere — that is exactly what `keybinding` prevents.
    for (const r of rows()) expect(r.hint ?? "").not.toMatch(/[⌘⇧⌥⌃]/);
  });
});

describe("session semantics — the decision, per command", () => {
  const CONVERSATION_SCOPED = ["clear", "export", "publish", "btw", "trajectory", "prompt", "perf", "compact"];
  const THREAD_ONLY = ["help", "effort", "model", "incognito", "bypass", "goal", "watch"];

  it("disables the commands that act on THIS conversation, and says why", () => {
    slot({ sessionId: null });
    for (const name of CONVERSATION_SCOPED) {
      const r = row(`chat-slash:${name}`);
      expect(r.disabled, `/${name} must not look runnable with no chat`).toBe(true);
      // The reason OUTRANKS every other hint, /btw's draft promise included: a dead row that
      // doesn't say why is worse than no row at all.
      expect(r.hint).toBe("needs an open chat"); // still LISTED, and explains itself
    }
  });

  it("keeps the commands that need only A thread runnable, promising the tab in the hint", () => {
    slot({ sessionId: null });
    for (const name of THREAD_ONLY) {
      const r = row(`chat-slash:${name}`);
      expect(r.disabled, `/${name} needs a thread, not this one — it can make one`).toBeFalsy();
      expect(r.hint).toBe("opens a chat first");
    }
    expect(row("chat-slash:new").disabled).toBeFalsy(); // needs nothing at all
    expect(row("chat-slash:new").hint).toBeUndefined(); // …and promises no tab it isn't making
  });

  it("re-enables every row the moment a session exists — the state is LIVE, never snapshotted", () => {
    slot({ sessionId: null });
    expect(rows().filter((r) => r.disabled)).toHaveLength(CONVERSATION_SCOPED.length);
    offs.pop()?.();
    slot({ sessionId: "sess-9" });
    expect(rows().filter((r) => r.disabled)).toEqual([]);
  });

  it("accounts for EVERY registered command — no row falls between the buckets", () => {
    expect(new Set([...CONVERSATION_SCOPED, ...THREAD_ONLY, "new"])).toEqual(
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
    // that dispatched "goal" would be the exact silent no-op this source exists to avoid.
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
    queryClient.setQueryData(queryKeys.chatCommands, {
      commands: [
        { name: "triage", description: "A skill.", kind: "skill" },
        { name: "research-and-brief", description: "A workflow.", kind: "workflow" },
        { name: "dream", description: "A subagent.", kind: "subagent" },
        { name: "lifecycle", description: "A control command.", kind: "control" },
      ],
    });
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

describe("the source is cheap, and tracks the LIVE list", () => {
  it("never fetches — it runs on every keystroke into the palette", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
    slot();
    skills("triage");
    for (let i = 0; i < 5; i++) rows(); // five keystrokes
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("follows the query CACHE, so enabling a plugin changes ⌘⇧K with no restart", () => {
    // /api/chat/commands is re-resolved per request, so the answer moves under a live
    // console. A snapshot taken at registration would list yesterday's skills forever.
    slot();
    skills("triage");
    expect(rows().map((r) => r.id)).toContain("chat-skill:triage");
    skills("triage", "postmortem");
    expect(rows().map((r) => r.id)).toContain("chat-skill:postmortem");
    skills();
    expect(rows().map((r) => r.id).filter((id) => id.startsWith("chat-skill:"))).toEqual([]);
  });

  it("registers as a DYNAMIC source, and withdraws cleanly", () => {
    slot();
    const off = registerChatSlashPalette(nav);
    expect(registeredPaletteCommands("dynamic").map((r) => r.id)).toContain("chat-slash:new");
    // Static registration would freeze both halves: the skill list is live server state and
    // the disabled flags track the chat slot's session.
    expect(registeredPaletteCommands("static").map((r) => r.id)).not.toContain("chat-slash:new");
    off();
    expect(registeredPaletteCommands("dynamic")).toEqual([]);
  });
});
