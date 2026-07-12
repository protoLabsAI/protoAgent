import { afterEach, describe, expect, it, vi } from "vitest";

import { findSlashCommand, type ComposerFormSpec, type SlashContext } from "../ext/slashRegistry";
import "./coreSlashCommands"; // side-effect: registers /model with the rest

import { queryClient } from "../lib/queryClient";
import { queryKeys } from "../lib/queries";
import type { SettingsGroup } from "../lib/types";

// The /model quick-switch (#1957): bare /model opens a card picker fed by the pinned
// favorites (settings schema), typed /model <alias> applies directly, /model default
// clears the tab override. The schema is read through the app's queryClient cache —
// seeded here, so no network and no React.

const field = (key: string, value: unknown, options: string[] = []) => ({
  key,
  label: key,
  type: "string",
  section: "Model",
  restart: false,
  options,
  value,
});

function seedSchema(favorites: string[], models: string[] = ["protolabs/reasoning", "protolabs/fast"]) {
  const groups = [
    { section: "Model", category: "Model", fields: [field("model.name", "protolabs/reasoning", models)] },
    { section: "Favorite models", category: "Model", fields: [field("model.favorites", favorites, models)] },
  ] as SettingsGroup[];
  queryClient.setQueryData(queryKeys.settings, { groups });
}

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: "s1", noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

const modelField = (spec: ComposerFormSpec): { oneOf: { const: string }[]; default?: string } =>
  (spec.payload.steps![0].schema as { properties: Record<string, unknown> }).properties.model as never;

afterEach(() => {
  queryClient.removeQueries({ queryKey: queryKeys.settings });
  queryClient.removeQueries({ queryKey: queryKeys.runtime });
});

describe("/model quick-switch (#1957)", () => {
  it("is registered through the same seam a fork uses", () => {
    expect(findSlashCommand("model")).toBeTruthy();
  });

  it("falls through (returns false) without a session", () => {
    expect(findSlashCommand("model")!.run(ctx({ sessionId: null }))).toBe(false);
  });

  it("bare /model opens the picker with ONLY the favorites, current model as the payload default", async () => {
    seedSchema(["protolabs/fast", "protolabs/reasoning"]);
    let spec: ComposerFormSpec | null = null;
    const handled = findSlashCommand("model")!.run(ctx({ openForm: (s) => (spec = s) }));
    expect(handled).toBe(true);
    await vi.waitFor(() => expect(spec).toBeTruthy());
    expect(modelField(spec!).oneOf.map((o) => o.const)).toEqual(["protolabs/fast", "protolabs/reasoning"]);
    // No per-tab override in the store → the payload default is the configured default
    // (parity with /effort's picker payload; HitlForm gates Submit on an explicit pick).
    expect(modelField(spec!).default).toBe("protolabs/reasoning");
  });

  it("no favorites → the FULL gateway list, with a pin-favorites hint", async () => {
    seedSchema([]);
    let spec: ComposerFormSpec | null = null;
    findSlashCommand("model")!.run(ctx({ openForm: (s) => (spec = s) }));
    await vi.waitFor(() => expect(spec).toBeTruthy());
    expect(modelField(spec!).oneOf.map((o) => o.const)).toEqual(["protolabs/reasoning", "protolabs/fast"]);
    expect(spec!.payload.description).toContain("No favorites pinned");
  });

  it("submitting a pick applies + notes it; the configured default resets the override; bogus is a no-op", async () => {
    seedSchema(["protolabs/fast", "protolabs/reasoning"]);
    let spec: ComposerFormSpec | null = null;
    let noted = "";
    findSlashCommand("model")!.run(ctx({ openForm: (s) => (spec = s), noteToThread: (m) => (noted = m) }));
    await vi.waitFor(() => expect(spec).toBeTruthy());
    spec!.onSubmit({ model: "protolabs/fast" });
    expect(noted).toContain("Model set to **protolabs/fast**");
    noted = "";
    spec!.onSubmit({ model: "protolabs/reasoning" }); // the configured default → clears the override
    expect(noted).toContain("reset to the configured default");
    noted = "";
    spec!.onSubmit({ model: "not/offered" }); // not among the cards → no-op
    expect(noted).toBe("");
  });

  it("typed /model <alias> applies directly (case-insensitive), never opening the form", async () => {
    seedSchema(["protolabs/fast"]);
    let noted = "";
    let opened = false;
    findSlashCommand("model")!.run(
      ctx({ rest: "PROTOLABS/FAST", noteToThread: (m) => (noted = m), openForm: () => (opened = true) }),
    );
    await vi.waitFor(() => expect(noted).toContain("Model set to **protolabs/fast**"));
    expect(opened).toBe(false);
  });

  it("typed /model default clears the override; an unknown alias warns", async () => {
    seedSchema(["protolabs/fast"]);
    let noted = "";
    findSlashCommand("model")!.run(ctx({ rest: "default", noteToThread: (m) => (noted = m) }));
    await vi.waitFor(() => expect(noted).toContain("reset to the configured default"));
    noted = "";
    findSlashCommand("model")!.run(ctx({ rest: "protolabs/typo", noteToThread: (m) => (noted = m) }));
    await vi.waitFor(() => expect(noted).toContain("Unknown model"));
    expect(noted).toContain("protolabs/fast"); // the known-favorites hint
  });

  it("degrades to a note when the host hasn't wired openForm (optional seam)", async () => {
    seedSchema(["protolabs/fast"]);
    let noted = "";
    findSlashCommand("model")!.run(ctx({ noteToThread: (m) => (noted = m) }));
    await vi.waitFor(() => expect(noted).toContain("Model for this tab:"));
    expect(noted).toContain("Settings ▸ Model");
  });

  it("under an ACP runtime the command explains itself instead of offering inert gateway models", () => {
    seedSchema(["protolabs/fast"]);
    queryClient.setQueryData(queryKeys.runtime, { agent_runtime: "acp:claude" });
    let noted = "";
    let opened = false;
    const handled = findSlashCommand("model")!.run(
      ctx({ noteToThread: (m) => (noted = m), openForm: () => (opened = true) }),
    );
    expect(handled).toBe(true);
    expect(opened).toBe(false);
    expect(noted).toContain("coding agent");
  });
});
