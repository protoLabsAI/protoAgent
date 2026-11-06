// Settings ▸ Chat ▸ "Starter prompts" (#4570, bd-4wss). The one console editor for the
// per-agent empty-chat chips. Covered here: the default read-only view, Customize, the
// add/remove/reorder row controls, the 8-row cap, validation blocking Save, the EXACT
// /api/settings payloads (a custom list, a toggle-off, a reset=null), and the ok:false
// error path (a refused save must not claim success).

import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { STARTER_PROMPTS } from "../chat/starterPrompts";
import { api } from "../lib/api";
import { runtimeStatusQuery } from "../lib/queries";
import type { ConfigPayload, RuntimeStatus } from "../lib/types";
import type { StarterPrompt } from "./StarterPromptsEditor";
import { StarterPromptsEditor } from "./StarterPromptsEditor";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const configWith = (chat?: ConfigPayload["config"]["chat"]): ConfigPayload =>
  ({ config: { chat }, soul: "" }) as unknown as ConfigPayload;

const runtimeWith = (prompts: StarterPrompt[]): RuntimeStatus =>
  ({ starter_prompts: prompts }) as unknown as RuntimeStatus;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.querySelectorAll(".pl-toast").forEach((t) => t.remove());
  vi.restoreAllMocks();
});

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function mount(opts: { chat?: ConfigPayload["config"]["chat"]; runtimePrompts?: StarterPrompt[] } = {}) {
  const payload = configWith(opts.chat);
  const runtime = runtimeWith(opts.runtimePrompts ?? [...STARTER_PROMPTS]);
  const saveSpy = vi
    .spyOn(api, "saveSettings")
    .mockResolvedValue({ ok: true, messages: [], restart_required: [] });
  vi.spyOn(api, "config").mockResolvedValue(payload);
  vi.spyOn(api, "runtimeStatus").mockResolvedValue(runtime);

  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["config"], payload);
  client.setQueryData(runtimeStatusQuery().queryKey, runtime);
  await act(async () => {
    root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(StarterPromptsEditor))));
  });
  await flush();
  return { saveSpy };
}

const byTestId = <T extends HTMLElement = HTMLElement>(id: string) =>
  container.querySelector<T>(`[data-testid="${id}"]`);

async function setValue(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!;
  await act(async () => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function type(id: string, value: string) {
  const el = byTestId<HTMLInputElement>(id);
  if (!el) throw new Error(`missing field ${id}`);
  await setValue(el, value);
}

async function click(id: string) {
  const el = byTestId<HTMLButtonElement>(id);
  if (!el) throw new Error(`missing button ${id}`);
  await act(async () => {
    el.click();
    await Promise.resolve();
  });
}

const labelValues = () =>
  [...container.querySelectorAll<HTMLInputElement>('[data-testid^="starter-label-"]')].map((i) => i.value);

// ─────────────────────────────────────────────────────────────────────────────

describe("StarterPromptsEditor", () => {
  it("default view lists the four built-ins read-only, offers Customize, and Save is disabled", async () => {
    await mount(); // no chat config

    expect(byTestId("starter-using-defaults")).not.toBeNull();
    const items = byTestId("starter-builtins")!.querySelectorAll("li");
    expect([...items].map((li) => li.querySelector(".setting-label")?.textContent)).toEqual([
      "Take a quick tour",
      "Summarise a web page",
      "Remember my preferences",
      "Ask Claude Code to add a test",
    ]);
    expect(byTestId("starter-customize")).not.toBeNull();
    expect(byTestId("starter-rows")).toBeNull(); // not in edit mode yet
    expect(byTestId<HTMLButtonElement>("starter-save")!.disabled).toBe(true); // clean form
  });

  it("Customize copies the built-ins into an editable list and enables Save", async () => {
    await mount({ runtimePrompts: [{ label: "A", prompt: "do a" }] });

    await click("starter-customize");
    expect(byTestId("starter-rows")).not.toBeNull();
    expect(labelValues()).toEqual(["A"]);
    // Pinning the defaults into an explicit list is itself a change → Save enabled.
    expect(byTestId<HTMLButtonElement>("starter-save")!.disabled).toBe(false);
  });

  it("adds, removes, and reorders rows", async () => {
    await mount({
      chat: {
        starter_prompts: [
          { label: "A", prompt: "pa" },
          { label: "B", prompt: "pb" },
          { label: "C", prompt: "pc" },
        ],
      },
    });
    expect(labelValues()).toEqual(["A", "B", "C"]);

    await click("starter-down-0"); // A ↓ → B, A, C
    expect(labelValues()).toEqual(["B", "A", "C"]);

    await click("starter-up-2"); // C ↑ → B, C, A
    expect(labelValues()).toEqual(["B", "C", "A"]);

    await click("starter-remove-1"); // drop C → B, A
    expect(labelValues()).toEqual(["B", "A"]);

    await click("starter-add"); // append a blank row
    expect(labelValues()).toEqual(["B", "A", ""]);
  });

  it("disables Add at the 8-row cap", async () => {
    const seven: StarterPrompt[] = Array.from({ length: 7 }, (_, i) => ({ label: `L${i}`, prompt: `p${i}` }));
    await mount({ chat: { starter_prompts: seven } });

    expect(byTestId<HTMLButtonElement>("starter-add")!.disabled).toBe(false);
    await click("starter-add"); // → 8 rows
    expect(labelValues()).toHaveLength(8);
    expect(byTestId<HTMLButtonElement>("starter-add")!.disabled).toBe(true);
  });

  it("blocks Save and warns when a row is missing its label or prompt", async () => {
    await mount({ chat: { starter_prompts: [{ label: "A", prompt: "pa" }] } });
    expect(byTestId("starter-blank-warning")).toBeNull();

    await type("starter-label-0", "   "); // blank after trim
    expect(byTestId("starter-blank-warning")).not.toBeNull();
    expect(byTestId<HTMLButtonElement>("starter-save")!.disabled).toBe(true);
  });

  it("saves a custom list — only the changed key, cleaned", async () => {
    const { saveSpy } = await mount({ runtimePrompts: [{ label: "A", prompt: "do a" }] });

    await click("starter-customize"); // 1 row: A / do a
    await click("starter-add"); // blank second row
    await type("starter-label-1", " Build ");
    await type("starter-prompt-1", " open a PR ");
    await type("starter-note-1", "  "); // blank note dropped on clean
    await click("starter-save");
    await flush();

    expect(saveSpy).toHaveBeenCalledTimes(1);
    expect(saveSpy).toHaveBeenCalledWith({
      "chat.starter_prompts": [
        { label: "A", prompt: "do a" },
        { label: "Build", prompt: "open a PR" },
      ],
    });
  });

  it("saves the toggle off without touching the list", async () => {
    const { saveSpy } = await mount(); // no chat → defaults, switch on

    const sw = byTestId<HTMLInputElement>("starter-show")!;
    await act(async () => {
      sw.click(); // toggle off
      await Promise.resolve();
    });
    await click("starter-save");
    await flush();

    expect(saveSpy).toHaveBeenCalledWith({ "chat.show_starter_prompts": false });
  });

  it("emptying every row saves chat.starter_prompts: [] (none shown, not the defaults)", async () => {
    const { saveSpy } = await mount({ chat: { starter_prompts: [{ label: "A", prompt: "pa" }] } });

    await click("starter-remove-0"); // now zero rows, still an explicit (empty) list
    expect(byTestId("starter-using-defaults")).toBeNull(); // NOT the default view
    await click("starter-save");
    await flush();

    expect(saveSpy).toHaveBeenCalledWith({ "chat.starter_prompts": [] });
  });

  it("reset to defaults saves chat.starter_prompts: null", async () => {
    const { saveSpy } = await mount({ chat: { starter_prompts: [{ label: "X", prompt: "px" }] } });

    await click("starter-reset");
    expect(byTestId("starter-using-defaults")).not.toBeNull(); // back to the default view
    await click("starter-save");
    await flush();

    expect(saveSpy).toHaveBeenCalledWith({ "chat.starter_prompts": null });
  });

  it("a refused save (ok:false) shows an error toast and does not claim success", async () => {
    const { saveSpy } = await mount({ chat: { starter_prompts: [{ label: "A", prompt: "pa" }] } });
    saveSpy.mockResolvedValue({ ok: false, messages: ["capped at 8"], restart_required: [] });

    await type("starter-label-0", "Edited");
    await click("starter-save");
    await flush();

    const toast = document.querySelector(".pl-toast");
    expect(toast).not.toBeNull();
    expect(toast!.classList.contains("pl-toast--error")).toBe(true);
    expect(document.querySelector(".pl-toast__msg")?.textContent).toContain("capped at 8");
    expect(document.querySelector(".pl-toast--success")).toBeNull();
  });
});
