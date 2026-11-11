// WorkDetail is mounted for real (inside the DS ToastProvider so the undo toast renders); only
// the action `run` / `onUndo` callbacks are stubbed. The inline confirm, focus moves, undo toast
// and the failure Alert are all exercised through the DOM, the way an operator drives them.

import { createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { WorkDetail, type WorkAction, type WorkDetailProps } from "./WorkDetail";
import { SurfaceFrameContext } from "../shell/SurfaceFrame";
import cssSource from "./work-detail.css?raw";
import componentSource from "./WorkDetail.tsx?raw";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

function base(overrides: Partial<WorkDetailProps> = {}): WorkDetailProps {
  return {
    title: "Ship the release",
    status: "running",
    fields: [],
    actions: [],
    technical: {},
    ...overrides,
  };
}

async function mount(props: WorkDetailProps) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(h(ToastProvider, null, h(WorkDetail, props) as ReactNode)));
}

async function flush() {
  await act(async () => {});
}

function buttons(): HTMLButtonElement[] {
  return Array.from(host!.querySelectorAll("button"));
}
function button(text: string): HTMLButtonElement {
  const found = buttons().find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`no button "${text}" (have: ${buttons().map((b) => b.textContent?.trim()).join(", ")})`);
  return found;
}
function text(): string {
  return host!.textContent ?? "";
}

const deleteAction = (run: WorkAction["run"]): WorkAction => ({
  id: "del",
  label: "Delete…",
  destructive: true,
  confirm: {
    prompt: "Delete this goal?",
    explanation: "The agent stops working toward it. Work it has already done stays.",
    confirmLabel: "Delete",
    keepLabel: "Keep it",
  },
  run,
});

beforeEach(() => {
  root = null;
  host = null;
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("WorkDetail ▸ header and fields", () => {
  it("shows the title, a status word and no empty fields (never a dash)", async () => {
    await mount(
      base({
        status: "needs-you",
        fields: [
          { label: "Owner", value: "you" },
          { label: "Blank", value: "" },
          { label: "Whitespace", value: "   " },
          { label: "Missing", value: null },
        ],
      }),
    );
    const heading = host!.querySelector<HTMLHeadingElement>("h2.work-detail__title")!;
    expect(heading.textContent).toBe("Ship the release");
    expect(heading.hasAttribute("data-surface-focus")).toBe(true);
    expect(text()).toContain("needs you"); // Badge word for needs-you
    expect(text()).toContain("Owner");
    expect(text()).toContain("you");
    expect(text()).not.toContain("Blank");
    expect(text()).not.toContain("Whitespace");
    expect(text()).not.toContain("Missing");
    expect(text()).not.toContain("—");
  });
});

describe("WorkDetail ▸ inline confirm (r1)", () => {
  it("opens a role=group labelled by its prompt, with the prompt focused, and runs nothing yet", async () => {
    const run = vi.fn(async () => undefined);
    await mount(base({ actions: [deleteAction(run)] }));

    await act(async () => button("Delete…").click());

    const group = host!.querySelector<HTMLElement>('[role="group"]')!;
    expect(group).toBeTruthy();
    const prompt = host!.querySelector<HTMLParagraphElement>(".work-detail__confirm-prompt")!;
    expect(prompt.textContent).toBe("Delete this goal?");
    expect(group.getAttribute("aria-labelledby")).toBe(prompt.id);
    expect(document.activeElement).toBe(prompt);
    expect(run).not.toHaveBeenCalled();
  });

  it("Keep it restores the row and returns focus to the trigger without running", async () => {
    const run = vi.fn(async () => undefined);
    await mount(base({ actions: [deleteAction(run)] }));

    await act(async () => button("Delete…").click());
    await act(async () => button("Keep it").click());

    expect(host!.querySelector('[role="group"]')).toBeNull();
    expect(run).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(button("Delete…"));
  });

  it("Escape restores the row and returns focus to the trigger without running", async () => {
    const run = vi.fn(async () => undefined);
    await mount(base({ actions: [deleteAction(run)] }));

    await act(async () => button("Delete…").click());
    await act(async () => {
      document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });

    expect(host!.querySelector('[role="group"]')).toBeNull();
    expect(run).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(button("Delete…"));
  });
});

describe("WorkDetail ▸ confirm, undo toast and restore (r2)", () => {
  it("confirming runs the action; an undoToken shows the undo toast; Undo restores", async () => {
    const run = vi.fn(async () => ({ undoToken: "tok-1" }));
    const onUndo = vi.fn(async () => {});
    await mount(base({ type: "Goal", actions: [deleteAction(run)], onUndo }));

    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();

    expect(run).toHaveBeenCalledTimes(1);
    expect(text()).toContain("Goal deleted.");

    await act(async () => button("Undo").click());
    await flush();

    expect(onUndo).toHaveBeenCalledWith("tok-1");
    expect(text()).toContain("Restored.");
  });

  it("a failed restore says the undo window has closed", async () => {
    const run = vi.fn(async () => ({ undoToken: "tok-2" }));
    const onUndo = vi.fn(async () => {
      throw new Error("gone");
    });
    await mount(base({ type: "Watch", actions: [deleteAction(run)], onUndo }));

    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();
    await act(async () => button("Undo").click());
    await flush();

    expect(text()).toContain("Couldn't restore it. The undo window has closed.");
  });
});

describe("WorkDetail ▸ failure (r3)", () => {
  it("a failed action shows an assertive warning Alert with Try again, and Try again re-runs", async () => {
    const run = vi
      .fn<WorkAction["run"]>()
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(undefined);
    await mount(base({ actions: [deleteAction(run)] }));

    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();

    const alert = host!.querySelector<HTMLElement>('[role="alert"]')!;
    expect(alert).toBeTruthy(); // role="alert" is announced assertively
    expect(alert.textContent).toContain("Couldn't delete it. Nothing changed.");

    await act(async () => button("Try again").click());
    await flush();

    expect(run).toHaveBeenCalledTimes(2);
    expect(host!.querySelector('[role="alert"]')).toBeNull();
  });

  it("a direct (non-confirm) action also surfaces the warning Alert on failure", async () => {
    const run = vi.fn(async () => {
      throw new Error("nope");
    });
    await mount(base({ actions: [{ id: "retry", label: "Retry", run }] }));

    await act(async () => button("Retry").click());
    await flush();

    const alert = host!.querySelector<HTMLElement>('[role="alert"]')!;
    expect(alert.textContent).toContain("Couldn't retry it. Nothing changed.");
  });
});

describe("WorkDetail ▸ technical details (r4)", () => {
  it("starts collapsed, then reveals a copyable value per entry", async () => {
    await mount(base({ technical: { id: "goal-42", session: "sess-9" } }));

    const trigger = button("Technical details");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(text()).not.toContain("goal-42");
    expect(buttons().some((b) => b.getAttribute("aria-label")?.startsWith("Copy "))).toBe(false);

    await act(async () => trigger.click());

    expect(text()).toContain("goal-42");
    expect(text()).toContain("sess-9");
    const copyButtons = buttons().filter((b) => b.getAttribute("aria-label")?.startsWith("Copy "));
    expect(copyButtons.map((b) => b.getAttribute("aria-label"))).toEqual(["Copy id", "Copy session"]);
  });
});

describe("WorkDetail ▸ changed notice", () => {
  it("renders an info Callout with Refresh when changedNotice is set", async () => {
    const onRefresh = vi.fn();
    await mount(base({ changedNotice: "This item changed while you were looking.", onRefresh }));
    expect(text()).toContain("This item changed while you were looking.");
    await act(async () => button("Refresh").click());
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe("WorkDetail ▸ tokens and no stacked dialog (r5)", () => {
  it("imports and renders no ConfirmDialog (inline confirm only)", () => {
    expect(componentSource).not.toMatch(/import[^;]*\bConfirmDialog\b/);
    expect(componentSource).not.toContain("<ConfirmDialog");
  });

  it("the stylesheet carries no literal px or colour and wraps technical values", () => {
    expect(cssSource).toContain("overflow-wrap: anywhere");
    expect(/\d(px|rem)\b/.test(cssSource)).toBe(false);
    expect(/#[0-9a-fA-F]{3,8}\b/.test(cssSource)).toBe(false);
    expect(/\b(rgb|rgba|hsl|hsla)\(/.test(cssSource)).toBe(false);
  });
});


it("uses actual md phone actions, including confirmation and retry, with destructive actions last", async () => {
  await mount(base());
  await act(async () => root!.render(h(SurfaceFrameContext.Provider, { value: "sheet" },
    h(ToastProvider, null, h(WorkDetail, base({ actions: [deleteAction(async () => { throw new Error("offline"); }), { id: "edit", label: "Edit", run: async () => {} }] }))))));
  expect(buttons().map(b => b.textContent)).toEqual(["Edit", "Delete…"]);
  expect(buttons().every(b => !b.classList.contains("pl-btn--sm"))).toBe(true);
  await act(async () => button("Delete…").click());
  await act(async () => button("Delete").click());
  expect(button("Try again").classList.contains("pl-btn--sm")).toBe(false);
  expect(button("Keep it").classList.contains("pl-btn--sm")).toBe(false);
});
