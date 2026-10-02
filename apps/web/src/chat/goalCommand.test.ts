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
