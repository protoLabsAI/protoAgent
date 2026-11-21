// DS audit card 4: the two hand-rolled `<input type="checkbox">` in the skill form
// became DS `Checkbox` (@protolabsai/ui/forms). This guards the two things a swap can
// silently break: the accessible NAME (each checkbox keeps its aria-label, so the DOM
// input still resolves by it) and the STATE LOGIC (unchecking the slash trigger also
// clears "user only", which requires a slash). SkillForm is a pure controlled component,
// so it renders standalone in jsdom — unlike the Suspense/fetch-driven PlaybooksBody.
import { act, createElement as h, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EMPTY_DRAFT, SkillForm, PlaybooksSurface } from "./PlaybooksSurface";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import type { Playbook } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
  vi.restoreAllMocks();
});

// A stateful wrapper: SkillForm is controlled, so the harness owns the draft and feeds
// setDraft back in — exactly how PlaybooksBody drives it.
function Harness({ initial }: { initial: typeof EMPTY_DRAFT }) {
  const [draft, setDraft] = useState(initial);
  return h(SkillForm, { draft, setDraft, onSave: () => {}, onCancel: () => {}, saving: false, saveLabel: "Create skill" });
}

function render(initial: typeof EMPTY_DRAFT) {
  act(() => root.render(h(Harness, { initial })));
}

const checkbox = (name: string) =>
  container.querySelector<HTMLInputElement>(`input[type="checkbox"][aria-label="${name}"]`);

describe("SkillForm checkboxes → DS Checkbox", () => {
  it("keeps each checkbox's aria-label accessible name and its visible /slash label", () => {
    render({ ...EMPTY_DRAFT, userFacing: true });
    const slash = checkbox("invokable as a slash command");
    const userOnly = checkbox("hide from the agent — operator slash command only");
    expect(slash).toBeTruthy();
    expect(userOnly).toBeTruthy();
    // Visible label text (with the <code>/slash</code> markup) is preserved alongside the aria-label.
    const slashLabel = slash!.closest(".pl-checkbox");
    expect(slashLabel?.textContent).toContain("Invokable as a");
    expect(slashLabel?.querySelector("code")?.textContent).toBe("/slash");
    expect(userOnly!.closest(".pl-checkbox")?.textContent).toContain("Hide from the agent");
  });

  it("no raw <input type=checkbox> renders — the swap uses the DS component", () => {
    render({ ...EMPTY_DRAFT, userFacing: true, userOnly: true });
    // Every checkbox in the tree carries the DS class, i.e. none is a hand-rolled bare input.
    for (const box of container.querySelectorAll('input[type="checkbox"]')) {
      expect(box.classList.contains("pl-checkbox__input")).toBe(true);
    }

  });

  it("unchecking the slash trigger clears user-only", () => {
    render({ ...EMPTY_DRAFT, userFacing: true, userOnly: true });
    expect(checkbox("hide from the agent — operator slash command only")?.checked).toBe(true);

    // Uncheck "invokable as a slash command" → userFacing false, so the user-only row unmounts…
    act(() => checkbox("invokable as a slash command")!.click());
    expect(checkbox("hide from the agent — operator slash command only")).toBeNull();

    // …and re-checking the slash trigger brings user-only back UNCHECKED, proving it was cleared.
    act(() => checkbox("invokable as a slash command")!.click());
    expect(checkbox("hide from the agent — operator slash command only")?.checked).toBe(false);
  });
});

const button = (name: string) => Array.from(document.querySelectorAll("button"))
  .find((el) => el.textContent?.trim() === name)!;
const flush = async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); };

async function mountSurface(enabled: boolean, playbooks: Playbook[] = []) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(queryKeys.playbooks, { enabled, playbooks });
  vi.spyOn(api, "playbooks").mockResolvedValue({ enabled, playbooks });
  await act(async () => {
    root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(PlaybooksSurface))));
  });
  return client;
}

function expectPlainCopy() {
  expect(document.body.textContent).not.toMatch(/commons|SKILL\.md|skills\.enabled|retrieves into context/i);
  for (const el of document.querySelectorAll("[title], [placeholder], [aria-label]")) {
    for (const attr of ["title", "placeholder", "aria-label"]) {
      expect(el.getAttribute(attr) ?? "").not.toMatch(/commons|SKILL\.md|skills\.enabled|retrieves into context/i);
    }
  }
}

describe("Skills settings", () => {
  it("explains the guides and shared tier in plain words", async () => {
    const skill = { id: 1, name: "Research", description: "Research a topic", tools_used: [],
      source: "disk", origin: "bundled", tier: "commons",
      confidence: 1, last_used: "", created_at: "" } as Playbook;
    await mountSurface(true, [skill]);
    expect(container.textContent).toContain("Step-by-step guides the agent can follow · 1 pinned · 0 learned · 1 shared");
    expect(container.textContent).toContain("Shared");
    expect(container.querySelector('[title="Built-in example. Read-only."]')).not.toBeNull();
    expectPlainCopy();
    await act(async () => container.querySelector<HTMLButtonElement>('[data-testid="playbook-forget-1"]')!.click());
    expect(document.body.textContent).toContain("Unshare this skill?");
    expectPlainCopy();
  });

  it("offers creation from the empty state", async () => {
    await mountSurface(true);
    expect(container.textContent).toContain("No skills yet");
    expect(container.textContent).toContain("Write one, or the agent can save one after a run.");
    expect(container.textContent).toContain("Step-by-step guides the agent can follow · 0 pinned · 0 learned");
    expectPlainCopy();
    await act(async () => button("New skill").click());
    expect(document.querySelector('[placeholder="The steps, in Markdown."]')).not.toBeNull();
    expect(button("Create skill")).toBeTruthy();
    expectPlainCopy();
  });

  it.each([false, true])("turns skills on through settings (restart: %s)", async (restart) => {
    const client = await mountSurface(false);
    const save = vi.spyOn(api, "saveSettings").mockResolvedValue({
      ok: true, messages: [], restart_required: restart ? ["skills.enabled"] : [],
    });
    vi.spyOn(api, "restartReadiness").mockResolvedValue({
      supervised: true, idle: true, busy: [], boot_id: "before", how_to: "",
    } as Awaited<ReturnType<typeof api.restartReadiness>>);
    vi.mocked(api.playbooks).mockResolvedValue({ enabled: !restart, playbooks: [] });
    expect(container.textContent).toContain("Skills are off.");
    expectPlainCopy();
    await act(async () => button("Turn on").click());
    await flush();
    expect(save).toHaveBeenCalledWith({ "skills.enabled": true }, "agent");
    expect(client.getQueryData(queryKeys.playbooks)).toEqual({ enabled: !restart, playbooks: [] });
    expect(api.playbooks).toHaveBeenCalled();
    if (restart) {
      expect(container.textContent).toContain("Skills will turn on after a restart.");
      expect(button("Restart now")).toBeTruthy();
      expect(button("Turn on").disabled).toBe(true);
      await act(async () => window.dispatchEvent(new Event("protoagent:settings-restarted")));
      expect(container.textContent).not.toContain("Skills will turn on after a restart.");
    } else {
      expect(container.textContent).toContain("No skills yet");
    }
    expectPlainCopy();
  });

  it.each(["refused", "network"])("keeps Turn on available after a %s failure", async (failure) => {
    await mountSurface(false);
    const save = vi.spyOn(api, "saveSettings");
    if (failure === "network") save.mockRejectedValue(new Error("offline"));
    else save.mockResolvedValue({ ok: false, messages: ["skills.enabled rejected"], restart_required: [] });
    await act(async () => button("Turn on").click());
    await flush();
    expect(document.body.textContent).toContain("Could not turn on skills. Try again.");
    expect(button("Turn on").disabled).toBe(false);
    expect(api.playbooks).not.toHaveBeenCalled();
    expectPlainCopy();
  });
});
