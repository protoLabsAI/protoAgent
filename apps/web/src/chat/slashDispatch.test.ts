// The imperative seam a client slash command is dispatched through from OUTSIDE the chat
// composer (ADR 0061). Same contract as escapeStop's handler seam — last-write-wins,
// guarded unregister, inert when nothing is registered — plus the two facts a caller needs
// before it offers a command: is a slot mounted, and does it have a session. Both matter
// because from outside there is no draft to fall through to, so a command that declines is
// a SILENT no-op; the session half of that contract (which commands decline, and which
// "succeed" into a note nobody sees) is pinned in coreSlashCommands.test.ts.
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  prefillChatDraft,
  registerSlashDispatcher,
  runSlashFromOutside,
  slashDispatchTarget,
} from "./slashDispatch";
import type { SlashDispatchTarget } from "./slashDispatch";

// The seam is module state; every test registers explicitly, so clear any leak between them.
const offs: (() => void)[] = [];
// `prefillDraft` defaults to a no-op so the cases below stay about the axis they test
// (dispatch, session, visibility) rather than restating every field of the target.
const register = (dispatcher: Omit<SlashDispatchTarget, "prefillDraft"> & { prefillDraft?: (t: string) => void }) => {
  const off = registerSlashDispatcher({ prefillDraft: () => {}, ...dispatcher });
  offs.push(off);
  return off;
};
afterEach(() => {
  while (offs.length) offs.pop()?.();
});

describe("slash dispatch seam", () => {
  it("returns false with no dispatcher registered — the caller falls back, nothing is swallowed", () => {
    expect(runSlashFromOutside("help")).toBe(false);
    expect(slashDispatchTarget()).toBeNull();
  });

  it("dispatches the raw command to the registered slot", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1", surfaceActive: true });
    expect(runSlashFromOutside("effort high")).toBe(true);
    expect(run).toHaveBeenCalledWith("effort high");
  });

  it("tolerates the leading slash so a display token can't read as 'no chat surface'", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1", surfaceActive: true });
    expect(runSlashFromOutside("/help")).toBe(true);
    expect(run).toHaveBeenCalledWith("help");
  });

  it("passes a command's own false through — declining is NOT the same as unhandled to the slot, but both mean 'fall back'", () => {
    // e.g. /clear with no session, or a flag-off command: runClientSlash returns false.
    // The `run` assertion is the load-bearing half: without it this passes just as well if
    // the seam short-circuits on a null sessionId instead of asking the slot — which would
    // make it lie about flag-off commands, and about the three that DO work session-less.
    const run = vi.fn(() => false);
    register({ run, sessionId: null, surfaceActive: true });
    expect(runSlashFromOutside("clear")).toBe(false);
    expect(run).toHaveBeenCalledWith("clear");
  });

  it("normalizes only the wrapper — the leading slash and surrounding space, never the args", () => {
    // A palette row hands over a display token ("/effort high") possibly padded; the slot's
    // runClientSlash splits on whitespace, so the arg tail has to survive intact.
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1", surfaceActive: true });
    expect(runSlashFromOutside("  /effort high  ")).toBe(true);
    expect(run).toHaveBeenCalledWith("effort high");
  });

  it("never dispatches an empty token", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1", surfaceActive: true });
    expect(runSlashFromOutside("   ")).toBe(false);
    expect(runSlashFromOutside("/")).toBe(false);
    expect(run).not.toHaveBeenCalled();
  });

  it("last write wins — the newly visible slot replaces the outgoing one", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    register({ run: a, sessionId: "a", surfaceActive: true });
    register({ run: b, sessionId: "b", surfaceActive: true });
    runSlashFromOutside("help");
    expect(a).not.toHaveBeenCalled();
    expect(b).toHaveBeenCalledTimes(1);
    expect(slashDispatchTarget()).toEqual({ sessionId: "b", surfaceActive: true });
  });

  it("a stale unregister never drops a fresh registration (cleanup-order safety)", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    const offA = register({ run: a, sessionId: "a", surfaceActive: true });
    register({ run: b, sessionId: "b", surfaceActive: true });
    // Slot A's effect cleanup can run AFTER slot B registered (React re-render ordering) —
    // it must only clear its own target.
    offA();
    expect(runSlashFromOutside("help")).toBe(true);
    expect(b).toHaveBeenCalledTimes(1);
    expect(slashDispatchTarget()).toEqual({ sessionId: "b", surfaceActive: true });
  });

  it("unregistering the live target makes the seam inert again", () => {
    const run = vi.fn(() => true);
    const off = register({ run, sessionId: "s1", surfaceActive: true });
    off();
    expect(runSlashFromOutside("help")).toBe(false);
    expect(run).not.toHaveBeenCalled();
    expect(slashDispatchTarget()).toBeNull();
  });
});

describe("slashDispatchTarget — what a caller must check before offering a command", () => {
  it("null means no chat slot is mounted at all", () => {
    expect(slashDispatchTarget()).toBeNull();
  });

  it("reports a HIDDEN chat surface — true from run() would draw where nobody can see it", () => {
    // The slot stays registered while the operator is on Settings or a plugin rail (#613 —
    // that is what lets the palette reach chat from anywhere), but the surface's <section>
    // is `display: none` there. Every command that answers through `noteToThread` (/help,
    // /perf, /trajectory, /prompt, /watch) and the two that open a composer picker (/effort,
    // /model) would run, return true, and show the operator nothing. So the projection has
    // to carry it: a caller raises the chat surface first, and this is how it knows to.
    register({ run: () => true, sessionId: "s1", surfaceActive: false });
    expect(slashDispatchTarget()).toEqual({ sessionId: "s1", surfaceActive: false });
  });

  it("keeps dispatching into a hidden surface — the seam reports, it does not veto", () => {
    // Refusing would break the case the registration exists for: /clear, /bypass and
    // friends are legitimate to fire without yanking the operator onto the chat rail. The
    // caller decides; the seam just refuses to hide the fact.
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1", surfaceActive: false });
    expect(runSlashFromOutside("clear")).toBe(true);
    expect(run).toHaveBeenCalledWith("clear");
  });

  it("tracks visibility across re-registrations, like sessionId", () => {
    // The slot re-registers every render, so raising the chat surface flips this on the
    // next one — a caller that raised the surface and then re-read must see the new answer.
    register({ run: () => true, sessionId: "s1", surfaceActive: false });
    register({ run: () => true, sessionId: "s1", surfaceActive: true });
    expect(slashDispatchTarget()).toEqual({ sessionId: "s1", surfaceActive: true });
  });

  it("reports a mounted slot that has NO session — session-scoped commands will decline", () => {
    register({ run: () => false, sessionId: null, surfaceActive: true });
    expect(slashDispatchTarget()).toEqual({ sessionId: null, surfaceActive: true });
  });

  it("reports the visible slot's session id", () => {
    register({ run: () => true, sessionId: "sess-42", surfaceActive: true });
    expect(slashDispatchTarget()).toEqual({ sessionId: "sess-42", surfaceActive: true });
  });

  it("tracks the CURRENT registration, not the first one (per-render re-registration)", () => {
    register({ run: () => true, sessionId: "sess-1", surfaceActive: true });
    register({ run: () => true, sessionId: "sess-2", surfaceActive: true });
    expect(slashDispatchTarget()).toEqual({ sessionId: "sess-2", surfaceActive: true });
  });
});

// The projection is only as honest as what the slot puts INTO it. Both fields are live
// per-render values in ChatSurface (`session?.id`, the `surfaceActive` prop); pinning a
// literal there — the easy "it's always visible when you'd dispatch" assumption — would
// restore the exact silent no-op this seam reports its way out of, and every test above
// would still pass because they register by hand.
const CHAT_SURFACE = (
  import.meta.glob("./ChatSurface.tsx", { query: "?raw", import: "default", eager: true }) as Record<
    string,
    string
  >
)["./ChatSurface.tsx"];

describe("the chat slot's registration (source guard)", () => {
  it("passes its live session and visibility, never a literal", () => {
    const call = CHAT_SURFACE.match(/registerSlashDispatcher\(\{[\s\S]*?\}\)/)?.[0];
    expect(call, "ChatSurface must register through registerSlashDispatcher({...})").toBeTruthy();
    expect(call).toContain("surfaceActive");
    expect(call).not.toMatch(/surfaceActive:\s*(true|false)\b/);
    expect(call).toContain("sessionId");
    expect(call).not.toMatch(/sessionId:\s*"/);
  });
});

// The draft half of the seam (#3285). A user-facing SKILL cannot be run from outside — the
// server rewrites the message on the next SEND — so the only honest outside action is to
// hand the operator the draft. Same contract as `run`: inert with nothing registered, and
// a false return is a real signal rather than a shrug.
describe("prefillChatDraft", () => {
  it("is inert with no slot registered, and says so", () => {
    expect(prefillChatDraft("/triage ")).toBe(false);
  });

  it("writes the draft into the CURRENT slot", () => {
    const stale = vi.fn();
    const live = vi.fn();
    register({ run: () => true, sessionId: "s1", surfaceActive: true, prefillDraft: stale });
    register({ run: () => true, sessionId: "s2", surfaceActive: true, prefillDraft: live });
    expect(prefillChatDraft("/triage ")).toBe(true);
    expect(live).toHaveBeenCalledWith("/triage ");
    expect(stale).not.toHaveBeenCalled();
  });

  it("passes the text through VERBATIM — the trailing space is the caret affordance", () => {
    const prefillDraft = vi.fn();
    register({ run: () => true, sessionId: "s1", surfaceActive: true, prefillDraft });
    prefillChatDraft("/research-and-brief ");
    expect(prefillDraft).toHaveBeenCalledWith("/research-and-brief ");
  });

  it("does NOT run the command — a skill row that executed would be a lie", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1", surfaceActive: true, prefillDraft: () => {} });
    prefillChatDraft("/triage ");
    expect(run).not.toHaveBeenCalled();
  });
});
