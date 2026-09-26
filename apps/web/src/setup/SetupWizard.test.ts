// The Setup Wizard's half of the shared two-step archetype flow: the "agent" step is the
// shared ArchetypePicker (cards only), the "setup" step is the shared ArchetypeSetupForm
// (the same component NewAgentPanel shows in its dialog) — name first, the bundle's
// config_inputs as real fields, Advanced collapsed. Back keeps every choice.
//
// jsdom + react-dom/client, React.createElement (no @testing-library), like the
// NewAgentPanel tests.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SetupWizard } from "./SetupWizard";
import { api } from "../lib/api";
import type { Archetype, ArchetypePreview } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ARCHETYPES: Archetype[] = [
  { id: "basic", label: "Basic", icon: "bot", blurb: "Plain", bundle: null, soul: "# Basic" },
  { id: "engineer", label: "Engineer", icon: "wrench", blurb: "Ships code", bundle: "https://example.com/engineer.git", soul: "# Engineer" },
];
const PREVIEW: ArchetypePreview = {
  id: "engineer",
  bundle: {
    kind: "bundle",
    name: "Engineer",
    members: [],
    mcp: [],
    secrets: [],
    config_inputs: [
      { key: "engineer.repo", label: "Start in a local repo", type: "path", help: "Registered as a project." },
      { key: "engineer.branch", label: "Branch", type: "string", required: true },
    ],
  },
};
const CONFIG = {
  config: {
    identity: { name: "protoagent", operator: "" },
    model: { provider: "openai", api_base: "https://gw/v1", name: "m" },
    middleware: {},
    subagents: {},
    knowledge: {},
    operator: {},
  },
  soul: "",
};

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.spyOn(api, "config").mockResolvedValue(CONFIG as never);
  vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: ARCHETYPES });
  vi.spyOn(api, "archetypePreview").mockImplementation(async (id: string) => (id === "engineer" ? PREVIEW : { id, bundle: null }));
  vi.spyOn(api, "pythonRuntime").mockResolvedValue({
    python: { needed: false, managed: true, managed_version: "", exe: "", baseline_installed: true, baseline_current: true, supported: true, target_version: "" },
    install: { state: "idle", pct: 0, message: "", error: null },
  });
  vi.spyOn(api, "oauthStatus").mockResolvedValue({} as never);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function tick(done: () => boolean) {
  for (let i = 0; i < 50 && !done(); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
  }
}
const button = (re: RegExp) => [...container.querySelectorAll<HTMLButtonElement>("button")].find((b) => re.test(b.textContent?.trim() ?? ""));
async function click(el: HTMLElement | undefined | null) {
  if (!el) throw new Error("nothing to click");
  await act(async () => {
    el.click();
  });
}
const radioFor = (v: string) => [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find((r) => r.value === v);
const nameInput = () => container.querySelector<HTMLInputElement>('input[aria-label="Agent name"]');
async function typeInto(input: HTMLInputElement | null, value: string) {
  if (!input) throw new Error("no input");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function mountToPicker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client: qc }, h(ToastProvider, null, h(SetupWizard, { open: true, projectPath: "", onProjectPathChange: () => {}, onFinished: () => {} }))),
    );
  });
  await tick(() => Boolean(button(/^Next/)) && !button(/^Next/)!.disabled);
  await click(button(/^Next/)); // welcome → agent (pick)
  await tick(() => Boolean(radioFor("engineer")));
}

describe("SetupWizard — the shared two-step archetype flow", () => {
  it("the pick step is the shared picker: cards only, no name field", async () => {
    await mountToPicker();
    expect(container.querySelector(".archetype-picker")).not.toBeNull();
    expect(radioFor("basic")).toBeTruthy();
    expect(nameInput()).toBeNull();
    expect(container.querySelector(".archetype-setup")).toBeNull();
  });

  it("the set-up step is the shared form: suggested name first, help lines, folder picker, gated Next", async () => {
    await mountToPicker();
    await click(radioFor("engineer"));
    await click(button(/^Next/));
    await tick(() => Boolean(container.textContent?.includes("Start in a local repo")));

    expect(container.querySelector(".archetype-setup")).not.toBeNull();
    expect(container.textContent).toContain("Set up Engineer");
    expect(nameInput()?.value).toBe("engineer");
    expect(container.textContent).toContain("Registered as a project.");
    expect(container.querySelector('.path-picker input[aria-label="Start in a local repo"]')).not.toBeNull();
    // The required answer gates Next until it's given (#2977) — no env fallback.
    expect(button(/^Next/)?.disabled).toBe(true);
    await typeInto(container.querySelector<HTMLInputElement>('input[aria-label="Branch"]'), "main");
    expect(button(/^Next/)?.disabled).toBe(false);
  });

  it("Back from set-up returns to the picker keeping the pick, the typed name and the answers", async () => {
    await mountToPicker();
    await click(radioFor("engineer"));
    await click(button(/^Next/));
    await tick(() => Boolean(container.querySelector('input[aria-label="Branch"]')));
    await typeInto(nameInput(), "forge");
    await typeInto(container.querySelector<HTMLInputElement>('input[aria-label="Branch"]'), "main");

    await click(button(/^Back/));
    expect(radioFor("engineer")?.checked).toBe(true);
    await click(button(/^Next/));
    expect(nameInput()?.value).toBe("forge");
    expect(container.querySelector<HTMLInputElement>('input[aria-label="Branch"]')?.value).toBe("main");
  });

  it("Custom opens Advanced so the persona editor is on screen", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({
      archetypes: [...ARCHETYPES, { id: "custom", label: "Custom", icon: "pen", blurb: "Write your own", bundle: null, soul: "# Mine" }],
    });
    await mountToPicker();
    await click(radioFor("custom"));
    await click(button(/^Next/));
    expect(container.querySelector<HTMLTextAreaElement>(".archetype-setup-soul")?.value).toBe("# Mine");
  });

  it("Basic keeps the configured identity name (no archetype suggestion)", async () => {
    await mountToPicker();
    await click(radioFor("engineer"));
    await click(radioFor("basic"));
    await click(button(/^Next/));
    expect(nameInput()?.value).toBe("protoagent");
  });
});
