import { describe, expect, it } from "vitest";

import { registerComposerAction, registeredComposerActions } from "./composerRegistry";
import type { ComposerActionContext } from "./composerRegistry";

describe("composer-action registry (ADR 0061)", () => {
  it("registers an action and exposes it", () => {
    registerComposerAction({ id: "tmpl", label: "Insert template", icon: null, run: () => {} });
    expect(registeredComposerActions().some((a) => a.id === "tmpl")).toBe(true);
  });

  it("first registration of an id wins (HMR-safe)", () => {
    registerComposerAction({ id: "dup", label: "first", icon: null, run: () => {} });
    registerComposerAction({ id: "dup", label: "second", icon: null, run: () => {} });
    expect(registeredComposerActions().find((a) => a.id === "dup")?.label).toBe("first");
  });

  it("ignores invalid registrations (no id / no run)", () => {
    registerComposerAction({ id: "", label: "x", icon: null, run: () => {} });
    // @ts-expect-error — missing run
    registerComposerAction({ id: "norun", label: "x", icon: null });
    expect(registeredComposerActions().some((a) => a.id === "")).toBe(false);
    expect(registeredComposerActions().some((a) => a.id === "norun")).toBe(false);
  });

  it("run receives the composer context", () => {
    let gotSid: string | null = "unset";
    registerComposerAction({
      id: "ctx",
      label: "c",
      icon: null,
      run: (ctx: ComposerActionContext) => {
        gotSid = ctx.sessionId;
      },
    });
    registeredComposerActions()
      .find((a) => a.id === "ctx")!
      .run({ sessionId: "s1", setDraft: () => {}, focusComposer: () => {}, noteToThread: () => {} });
    expect(gotSid).toBe("s1");
  });
});
