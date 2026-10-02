import { describe, it, expect, vi } from "vitest";

import { findSlashCommand } from "../ext/slashRegistry";
import "./coreSlashCommands";

// Probe: does the client /goal command intercept `/goal new` and open the form,
// while letting bare/`<text>`/`clear` fall through to the server (return false)?
describe("/goal client interception", () => {
  const goal = findSlashCommand("goal");

  const ctx = (rest: string, extra: Record<string, unknown> = {}) =>
    ({
      rest,
      sessionId: "s1",
      noteToThread: vi.fn(),
      setDraft: vi.fn(),
      focusComposer: vi.fn(),
      openForm: vi.fn(),
      flagOn: () => true,
      serverCommands: [],
      ...extra,
    }) as never;

  it("is registered", () => {
    expect(goal).toBeTruthy();
  });

  it("/goal new opens the form and handles it (returns true, never sent)", async () => {
    const openForm = vi.fn();
    // The form's verifier options come from `GET /api/verifiers`, so the open lands a
    // microtask later — `run` still returns true SYNCHRONOUSLY, which is what stops the
    // literal text being sent. With no server here the fetch rejects and the form still
    // opens, on the core types.
    const handled = goal!.run(ctx("new", { openForm }));
    expect(handled).toBe(true);
    await vi.waitFor(() => expect(openForm).toHaveBeenCalledTimes(1));
  });

  it("bare /goal falls through to the server (returns false)", () => {
    expect(goal!.run(ctx(""))).toBe(false);
  });

  it("/goal <text> falls through to the server (returns false)", () => {
    expect(goal!.run(ctx("make the build green"))).toBe(false);
  });
});

// `/goal new` drives the goal IN THIS TAB (ADR 0090 D1): the set goes out with `kick: false`
// and the tab registers its own hidden kickoff, so the loop streams live here — the default
// headless kick ran it as a server-fired turn that surfaced only as a collapsed card.
describe("/goal new submit drives in this tab", () => {
  it("sets with kick:false, then registers this tab's kickoff", async () => {
    const { api } = await import("../lib/api");
    const { takeGoalKickoff } = await import("./chat-store");
    const setGoal = vi.spyOn(api, "setGoal").mockResolvedValue({ ok: true, message: "goal set" } as never);
    vi.spyOn(api, "verifiers").mockRejectedValue(new Error("offline"));
    const openForm = vi.fn();
    const noteToThread = vi.fn();
    const goal = findSlashCommand("goal");
    goal?.run({
      rest: "new",
      sessionId: "tab-7",
      noteToThread,
      setDraft: vi.fn(),
      focusComposer: vi.fn(),
      openForm,
      flagOn: () => true,
      serverCommands: [],
    } as never);
    await vi.waitFor(() => expect(openForm).toHaveBeenCalledTimes(1));

    openForm.mock.calls[0][0].onSubmit({ condition: "tests pass", verifier: "command", verify_command: "pytest -q" });
    await vi.waitFor(() => expect(noteToThread).toHaveBeenCalled());

    expect(setGoal.mock.calls[0][0]).toMatchObject({ session_id: "tab-7", condition: "tests pass", kick: false });
    expect(takeGoalKickoff("tab-7")).toContain("tests pass");
    vi.restoreAllMocks();
  });
});

// Review of #4008: `/goal new` reads the tab's busy state BEFORE the set-goal POST. If the
// operator starts a turn while the POST is in flight, the set goes out `kick:false` and the
// kickoff lands while the tab is streaming. The slot's consumer (watchGoalKickoff) must defer
// it until the tab is idle — never a second concurrent turn, never dropped, never run twice.
describe("/goal new kickoff while the tab turned busy mid-request", () => {
  it("defers the kickoff until idle, then runs it exactly once", async () => {
    const { api } = await import("../lib/api");
    const { chatStore } = await import("./chat-store");
    const { watchGoalKickoff } = await import("./goalKickoffWatch");
    const session = chatStore.createSession();
    const sid = session.id;
    let release!: () => void;
    vi.spyOn(api, "setGoal").mockImplementation(
      () => new Promise((resolve) => (release = () => resolve({ ok: true, message: "goal set" } as never))),
    );
    vi.spyOn(api, "verifiers").mockRejectedValue(new Error("offline"));

    // The slot's consumer, with a runTurn that records overlap the way the real one would
    // collide: it flips the tab to streaming for the turn's duration.
    let concurrent = 0;
    const run = vi.fn((_prompt: string) => {
      if (chatStore.getSnapshot().sessionStatusMap[sid] === "streaming") concurrent += 1;
      chatStore.setSessionStatus(sid, "streaming");
    });
    const off = watchGoalKickoff(sid, run).stop;

    const openForm = vi.fn();
    const noteToThread = vi.fn();
    findSlashCommand("goal")!.run({
      rest: "new",
      sessionId: sid,
      noteToThread,
      setDraft: vi.fn(),
      focusComposer: vi.fn(),
      openForm,
      flagOn: () => true,
      serverCommands: [],
    } as never);
    await vi.waitFor(() => expect(openForm).toHaveBeenCalledTimes(1));
    openForm.mock.calls[0][0].onSubmit({ condition: "tests pass", verifier: "command", verify_command: "pytest -q" });
    expect(api.setGoal).toHaveBeenCalledWith(expect.objectContaining({ kick: false }));

    // The operator sends a message while the POST is in flight → the tab is streaming.
    chatStore.setSessionStatus(sid, "streaming");
    release();
    await vi.waitFor(() => expect(noteToThread).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 10));
    expect(run).not.toHaveBeenCalled(); // deferred, not fired into the busy tab

    // The operator's turn ends → the kickoff runs once, on an idle tab.
    chatStore.setSessionStatus(sid, "idle");
    await vi.waitFor(() => expect(run).toHaveBeenCalledTimes(1));
    expect(run.mock.calls[0][0]).toContain("tests pass");
    expect(concurrent).toBe(0);

    // More store churn (the kickoff's own turn ending, other updates) never re-fires it.
    chatStore.setSessionStatus(sid, "idle");
    await new Promise((r) => setTimeout(r, 10));
    expect(run).toHaveBeenCalledTimes(1);
    off();
    vi.restoreAllMocks();
  });

  it("a kickoff registered on an idle tab still fires (Work-panel flow)", async () => {
    const { chatStore, registerGoalKickoff } = await import("./chat-store");
    const { watchGoalKickoff } = await import("./goalKickoffWatch");
    const sid = chatStore.createSession().id;
    chatStore.setSessionStatus(sid, "idle");
    const run = vi.fn();
    const off = watchGoalKickoff(sid, run).stop;
    registerGoalKickoff(sid, "Start working toward the goal: ship it");
    await vi.waitFor(() => expect(run).toHaveBeenCalledTimes(1));
    off();
  });
});

// Adversarial review of #4009: the kickoff watcher treated only "streaming" as busy. Two
// other owners of "the next turn" raced it at turn end — the steer reconcile re-sending a
// queued message, and a turn PARKED on the operator (a pending interrupt).
describe("goal kickoff waits for every owner of the next turn", () => {
  const tick = (ms = 10) => new Promise((r) => setTimeout(r, ms));

  async function setup() {
    const { chatStore, registerGoalKickoff } = await import("./chat-store");
    const { watchGoalKickoff } = await import("./goalKickoffWatch");
    const { beginLocalTurn, localTurnInFlight } = await import("./sessionLiveness");
    const sid = chatStore.createSession().id;
    // Make the session non-pristine so createSession hands the next test a fresh one.
    chatStore.updateMessages(sid, [{ id: `u-${sid}`, role: "user", content: "hi", createdAt: 1, status: "done" }]);
    let concurrent = 0;
    const turn = () => {
      const snap = chatStore.getSnapshot();
      if (snap.sessionStatusMap[sid] === "streaming" || localTurnInFlight(sid)) concurrent += 1;
      chatStore.setSessionStatus(sid, "streaming");
    };
    const run = vi.fn((_prompt: string) => turn());
    return { chatStore, registerGoalKickoff, watchGoalKickoff, beginLocalTurn, sid, run, turn, concurrent: () => concurrent };
  }

  it("a queued message re-sent by the turn-end reconcile goes first; the kickoff runs once, after", async () => {
    const t = await setup();
    const queue = ["queued while the goal POST was in flight"];
    t.chatStore.setSessionStatus(t.sid, "streaming");
    const w = t.watchGoalKickoff(t.sid, t.run, () => queue.length > 0); // the slot's blocked(): steer queue
    t.registerGoalKickoff(t.sid, "Start working toward the goal: tests pass");
    t.chatStore.setSessionStatus(t.sid, "idle"); // the operator's turn ends…
    await tick(5); // …and the reconcile's pendingSteer fetch returns 5ms later
    queue.length = 0;
    t.turn(); // reconcile re-sends the queued message as a fresh turn
    w.poke(); // reconcileSteer().finally(poke)
    await tick();
    expect(t.run).not.toHaveBeenCalled();
    t.chatStore.setSessionStatus(t.sid, "idle"); // that turn ends
    await vi.waitFor(() => expect(t.run).toHaveBeenCalledTimes(1));
    expect(t.concurrent()).toBe(0);
    w.stop();
  });

  it("a turn parked on the operator keeps the kickoff queued until it is answered", async () => {
    const t = await setup();
    const w = t.watchGoalKickoff(t.sid, t.run);
    t.chatStore.setSessionStatus(t.sid, "streaming");
    t.registerGoalKickoff(t.sid, "Start working toward the goal: tests pass");
    const base = t.chatStore.getSnapshot().sessions.find((s) => s.id === t.sid)!.messages;
    const parked = { id: "a-park", role: "assistant" as const, content: "Approve?", createdAt: 2, status: "streaming" as const, paused: true, taskId: "task-1" };
    t.chatStore.updateMessages(t.sid, [...base, parked]);
    t.chatStore.setSessionStatus(t.sid, "idle"); // the paused-turn path idles the session
    await tick();
    expect(t.run).not.toHaveBeenCalled(); // never abandons the pending interrupt
    // The operator answers; the resumed turn settles the bubble → the kickoff runs once.
    t.chatStore.updateMessages(t.sid, [...base, { ...parked, status: "done" as const, paused: false }]);
    await vi.waitFor(() => expect(t.run).toHaveBeenCalledTimes(1));
    expect(t.concurrent()).toBe(0);
    w.stop();
  });

  it("idle set inside a still-unwinding local turn waits for its finally", async () => {
    const t = await setup();
    const w = t.watchGoalKickoff(t.sid, t.run);
    t.chatStore.setSessionStatus(t.sid, "streaming");
    const endLocalTurn = t.beginLocalTurn(t.sid);
    t.registerGoalKickoff(t.sid, "Start working toward the goal: tests pass");
    t.chatStore.setSessionStatus(t.sid, "idle");
    await tick();
    expect(t.run).not.toHaveBeenCalled();
    endLocalTurn();
    w.poke(); // runTurn's finally pokes after releasing its claim
    await vi.waitFor(() => expect(t.run).toHaveBeenCalledTimes(1));
    expect(t.concurrent()).toBe(0);
    w.stop();
  });
});

// The server's set ack already starts with "Goal set." (SET_ACK_PREFIX); the note adds its
// own bold label, so the ack's prefix is dropped — it used to read "Goal set. Goal set. …".
describe("/goal new prints 'Goal set.' once", () => {
  it("strips the server ack's own prefix from the note", async () => {
    const { api } = await import("../lib/api");
    const ack = "Goal set. goal [active] via command: 'tests pass' (iteration 0/8)";
    vi.spyOn(api, "setGoal").mockResolvedValue({ ok: true, message: ack } as never);
    vi.spyOn(api, "verifiers").mockRejectedValue(new Error("offline"));
    const openForm = vi.fn();
    const noteToThread = vi.fn();
    findSlashCommand("goal")?.run({
      rest: "new",
      sessionId: "tab-8",
      noteToThread,
      setDraft: vi.fn(),
      focusComposer: vi.fn(),
      openForm,
      flagOn: () => true,
      serverCommands: [],
    } as never);
    await vi.waitFor(() => expect(openForm).toHaveBeenCalledTimes(1));
    openForm.mock.calls[0][0].onSubmit({ condition: "tests pass", verifier: "command", verify_command: "pytest -q" });
    await vi.waitFor(() => expect(noteToThread).toHaveBeenCalled());

    const note = String(noteToThread.mock.calls[0][0]);
    expect(note).toBe("**Goal set.** goal [active] via command: 'tests pass' (iteration 0/8)");
    expect(note.match(/goal set/gi)).toHaveLength(1);
    const { takeGoalKickoff } = await import("./chat-store");
    takeGoalKickoff("tab-8");
    vi.restoreAllMocks();
  });

  it("goalSetDetail drops only a leading ack prefix", async () => {
    const { goalSetDetail } = await import("./goalForm");
    expect(goalSetDetail("Goal set. goal [active] (iteration 0/8)")).toBe("goal [active] (iteration 0/8)");
    expect(goalSetDetail("goal [active] (iteration 0/8)")).toBe("goal [active] (iteration 0/8)");
    expect(goalSetDetail(undefined)).toBe("");
    expect(goalSetDetail("Goal set.")).toBe("");
    // A word that merely starts with "goal set" is not the ack's prefix.
    expect(goalSetDetail("Goal settings saved")).toBe("Goal settings saved");
  });
});
