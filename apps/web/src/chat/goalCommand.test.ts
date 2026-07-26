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
