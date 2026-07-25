// /prompt (#2243) — client-side, operator-only by construction: it
// short-circuits before the agent ever sees it and posts ephemeral notes.
// Mocks only api.promptLast (importOriginal keeps the rest of the api real —
// this file registers every core command at import).

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SlashContext } from "../ext/slashRegistry";
import { findSlashCommand } from "../ext/slashRegistry";
import { api } from "../lib/api";
import "./coreSlashCommands";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return { ...mod, api: { ...mod.api, promptLast: vi.fn() } };
});

const promptLast = vi.mocked(api.promptLast);

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return {
    rest: "",
    sessionId: "s1",
    noteToThread: () => {},
    setDraft: () => {},
    focusComposer: () => {},
    ...over,
  };
}

function call(over: Partial<SlashContext> = {}) {
  const noted: { md: string; tone?: string }[] = [];
  const handled = findSlashCommand("prompt")!.run(
    ctx({ noteToThread: (md, opts) => noted.push({ md, tone: opts?.tone }), ...over }),
  );
  return { handled, noted };
}

beforeEach(() => {
  promptLast.mockReset();
});

describe("/prompt", () => {
  it("falls through without a session", () => {
    promptLast.mockResolvedValue({ enabled: true, call: null });
    expect(call({ sessionId: null }).handled).toBe(false);
    expect(promptLast).not.toHaveBeenCalled();
  });

  it("notes the last captured call's prompt, fenced and labeled", async () => {
    promptLast.mockResolvedValue({
      enabled: true,
      call: {
        call_index: 0,
        ts: "2026-07-24T10:00:00+00:00",
        model: "claude-opus-4-7",
        system: { stable: "STABLE", context: "\n\n# Context\n\ntail" },
        usage: { input_tokens: 10, output_tokens: 2, cache_read_tokens: 0, cache_creation_tokens: 0 },
      },
    });
    const { handled, noted } = call();
    expect(handled).toBe(true);
    expect(promptLast).toHaveBeenCalledWith("s1");
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("info");
    expect(noted[0].md).toContain("**System prompt**");
    expect(noted[0].md).toContain("STABLE\n\n# Context\n\ntail");
  });

  it("says so when capture is off", async () => {
    promptLast.mockResolvedValue({ enabled: false, call: null });
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("warning");
    expect(noted[0].md).toContain("prompts.capture");
  });

  it("says so when nothing was captured yet", async () => {
    promptLast.mockResolvedValue({ enabled: true, call: null });
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("info");
    expect(noted[0].md).toContain("Nothing captured");
  });

  it("surfaces fetch failures as a danger note", async () => {
    promptLast.mockRejectedValue(new Error("boom"));
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("danger");
    expect(noted[0].md).toContain("boom");
  });
});
