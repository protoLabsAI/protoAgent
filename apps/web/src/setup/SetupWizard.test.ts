// Model-first wizard with identity, archetype, and bundle configuration in one step.
// jsdom + react-dom/client, React.createElement (no @testing-library), like the
// NewAgentPanel tests.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { agentStepReady, steps, brainStepReady, defaultState, hydrateState, pickLocalEndpoint, SetupWizard, testKey, UNCONFIGURED_API_BASE, withTimeout } from "./SetupWizard";
import type { LocalEndpoint } from "./SetupWizard";
import { primaryArchetypes } from "./ArchetypePicker";
import { api } from "../lib/api";
import type { Archetype, ArchetypePreview, ConfigPayload } from "../lib/types";

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
  vi.spyOn(api, "models").mockResolvedValue({ models: ["m"] } as never);
  vi.spyOn(api, "testModel").mockResolvedValue({ ok: true } as never);
  vi.spyOn(api, "config").mockResolvedValue(CONFIG as never);
  vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: ARCHETYPES });
  vi.spyOn(api, "archetypePreview").mockImplementation(async (id: string) => (id === "engineer" ? PREVIEW : { id, bundle: null }));
  vi.spyOn(api, "pythonRuntime").mockResolvedValue({
    python: { needed: false, managed: true, managed_version: "", exe: "", baseline_installed: true, baseline_current: true, supported: true, target_version: "" },
    install: { state: "idle", pct: 0, message: "", error: null },
  });
  vi.spyOn(api, "oauthStatus").mockResolvedValue({} as never);
  vi.spyOn(api, "localModels").mockResolvedValue({ endpoints: [] } as never);
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

async function mountToBrain() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client: qc }, h(ToastProvider, null, h(SetupWizard, { open: true, projectPath: "", onProjectPathChange: () => {}, onFinished: () => {} }))),
    );
  });
  await tick(() => Boolean(button(/^Next/)) && !button(/^Next/)!.disabled);
  await click(button(/^Next/)); // welcome → brain
  await tick(() => Boolean(radioFor("gateway")));
}

async function mountToPicker(expand = true) {
  await mountToBrain();
  await click(button(/^Test connection/));
  await tick(() => !button(/^Next/)?.disabled);
  await click(button(/^Next/)); // brain → agent
  await tick(() => Boolean(radioFor("basic")));
  if (expand) await click(button(/^More archetypes/));
}

describe("SetupWizard — the combined agent step", () => {
  it("defaults to Basic and an editable name, with four progress markers", async () => {
    await mountToPicker(false);
    expect(container.querySelectorAll(".setup-progress span")).toHaveLength(4);
    expect(radioFor("basic")?.checked).toBe(true);
    expect(radioFor("engineer")).toBeUndefined();
    expect(nameInput()?.value).toBe("protoagent");
    await typeInto(nameInput(), "  ");
    expect(button(/^Next/)?.disabled).toBe(true);
    await typeInto(nameInput(), "my-agent");
    await click(button(/^More archetypes/));
    await click(radioFor("engineer"));
    expect(nameInput()?.value).toBe("my-agent");
  });

  it("the set-up step is the shared form: suggested name first, help lines, folder picker, gated Next", async () => {
    await mountToPicker();
    await click(radioFor("engineer"));
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

  it("holds Next on the set-up step while the bundle's peek is loading (#3632)", async () => {
    let resolvePeek: (p: ArchetypePreview) => void = () => {};
    vi.spyOn(api, "archetypePreview").mockImplementation((id: string) =>
      id === "engineer" ? new Promise<ArchetypePreview>((r) => (resolvePeek = r)) : Promise.resolve({ id, bundle: null }),
    );
    await mountToPicker();
    await click(radioFor("engineer"));
    expect(nameInput()?.value).toBe("engineer");
    // Name is set, nothing is known to be missing yet — but the questions haven't landed.
    expect(button(/^Next/)?.disabled).toBe(true);

    await act(async () => resolvePeek({ ...PREVIEW, bundle: { ...PREVIEW.bundle!, config_inputs: [PREVIEW.bundle!.config_inputs![0]] } }));
    await tick(() => Boolean(container.textContent?.includes("Start in a local repo")));
    expect(button(/^Next/)?.disabled).toBe(false);
  });

  it("a FAILED peek keeps Next held on the set-up step and offers a Retry that refetches (#3632)", async () => {
    const peek = vi
      .spyOn(api, "archetypePreview")
      .mockImplementation(async (id: string) => {
        if (id === "engineer") throw new Error("peek exploded");
        return { id, bundle: null };
      });
    await mountToPicker();
    await click(radioFor("engineer"));
    // The wizard's query retries once (~1s) before it settles into the error state.
    for (let i = 0; i < 40 && !container.querySelector('[role="alert"]'); i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 100));
      });
    }
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("peek exploded");
    expect(nameInput()?.value).toBe("engineer");
    expect(button(/^Next/)?.disabled).toBe(true);

    const calls = peek.mock.calls.filter(([id]) => id === "engineer").length;
    const landed = { ...PREVIEW, bundle: { ...PREVIEW.bundle!, config_inputs: [PREVIEW.bundle!.config_inputs![0]] } };
    peek.mockImplementation(async (id: string) => (id === "engineer" ? landed : { id, bundle: null }));
    await click(button(/^Retry$/));
    await tick(() => Boolean(container.textContent?.includes("Start in a local repo")));
    expect(peek.mock.calls.filter(([id]) => id === "engineer").length).toBe(calls + 1);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(button(/^Next/)?.disabled).toBe(false);
  }, 10_000);

  it("Back from agent returns to the model keeping the pick, the typed name and the answers", async () => {
    await mountToPicker();
    await click(radioFor("engineer"));
    await tick(() => Boolean(container.querySelector('input[aria-label="Branch"]')));
    await typeInto(nameInput(), "forge");
    await typeInto(container.querySelector<HTMLInputElement>('input[aria-label="Branch"]'), "main");

    await click(button(/^Back/));
    expect(radioFor("gateway")?.checked).toBe(true);
    await click(button(/^Next/));
    expect(radioFor("engineer")?.checked).toBe(true);
    expect(nameInput()?.value).toBe("forge");
    expect(container.querySelector<HTMLInputElement>('input[aria-label="Branch"]')?.value).toBe("main");
  });

  it("Custom opens Advanced so the persona editor is on screen", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({
      archetypes: [...ARCHETYPES, { id: "custom", label: "Custom", icon: "pen", blurb: "Write your own", bundle: null, soul: "# Mine" }],
    });
    await mountToPicker();
    await click(radioFor("custom"));
    expect(container.querySelector<HTMLTextAreaElement>(".archetype-setup-soul")?.value).toBe("# Mine");
  });

  it("Basic keeps the configured identity name (no archetype suggestion)", async () => {
    await mountToPicker();
    await click(radioFor("engineer"));
    await click(radioFor("basic"));
    expect(nameInput()?.value).toBe("protoagent");
  });
});

// Partial /api/config payloads (a proxied fleet member on an older core) must not crash
// the first-run render — every missing section falls back to defaultState().
// #4565: the brain-step subscription cards must read as "sign in with your plan, no API
// key" — new users were reading the old "uses your Claude Code / Codex CLI login" copy as
// "install the CLI first", when sign-in actually happens in the browser from this callout.
describe("SetupWizard — subscription copy (#4565)", () => {

  it("both subscription cards say sign-in-with-your-plan and no API key — never 'CLI login'", async () => {
    await mountToBrain();
    const text = container.textContent ?? "";
    expect(text).toContain("Sign in with your Claude Pro/Max account. No API key needed.");
    expect(text).toContain("Sign in with your ChatGPT Plus/Pro account. No API key needed.");
    expect(text).not.toContain("Claude Code login");
    expect(text).not.toContain("Codex CLI login");
  });

  it("the Not-signed-in callout says sign-in happens here in the browser with no CLI", async () => {
    await mountToBrain();
    await click(radioFor("claude")); // switch to the Claude subscription (OAuth) brain
    await tick(() => (container.textContent ?? "").includes("Not signed in"));
    const text = container.textContent ?? "";
    expect(text).toContain("Not signed in");
    expect(text).toContain("no command-line tool and no separate login needed");
  });
});

describe("hydrateState", () => {
  const partial = (config: Record<string, unknown>) => ({ config, soul: "" }) as unknown as ConfigPayload;

  it("an empty config hydrates to the defaults", () => {
    expect(hydrateState(partial({}))).toEqual(defaultState());
  });

  it("missing sections fall back per section; present values win", () => {
    const s = hydrateState(
      partial({
        identity: { name: "forge" },
        model: { name: "gw:big", temperature: 0 },
        middleware: { audit: false },
      }),
    );
    const d = defaultState();
    expect(s.agentName).toBe("forge");
    expect(s.operatorName).toBe(d.operatorName);
    expect(s.modelName).toBe("gw:big");
    expect(s.temperature).toBe(0);
    expect(s.maxTokens).toBe(d.maxTokens);
    expect(s.apiBase).toBe(d.apiBase);
    expect(s.middleware).toEqual({ ...d.middleware, audit: false });
    expect(s.researcherTurns).toBe(d.researcherTurns);
    expect(s.knowledgeTopK).toBe(d.knowledgeTopK);
  });

  it("a fresh install's example endpoint hydrates blank, not as an unreachable pre-fill", () => {
    const s = hydrateState(partial({ model: { api_base: UNCONFIGURED_API_BASE, name: "protolabs/reasoning", temperature: 0.2 } }));
    expect(s.apiBase).toBe("");
    expect(s.modelName).toBe("");
    expect(s.temperature).toBe(0.2);
  });

  it("a configured endpoint and model are kept", () => {
    const s = hydrateState(partial({ model: { api_base: "https://api.openai.com/v1", name: "gpt-x" } }));
    expect(s.apiBase).toBe("https://api.openai.com/v1");
    expect(s.modelName).toBe("gpt-x");
  });
});

// The brain-step Next gate (#4562): a passing connection test — bound to the exact
// inputs via testKey — now gates Next, plus signed-in for subscriptions.
describe("brainStepReady", () => {
  const GW = {
    runtimeKind: "native" as const,
    provider: "openai",
    apiBase: "https://gw/v1",
    apiKey: "sk-1",
    modelName: "m",
  };
  const gwKey = testKey(GW);
  const gwArgs = { runtimeKind: GW.runtimeKind, provider: GW.provider, apiBase: GW.apiBase, modelName: GW.modelName, signedIn: false };

  it("gateway: blocked with no test, blocked on a failed test, unlocked on a pass for the current key", () => {
    expect(brainStepReady({ ...gwArgs, currentKey: gwKey, tested: null })).toBe(false);
    expect(brainStepReady({ ...gwArgs, currentKey: gwKey, tested: { ok: false, key: gwKey } })).toBe(false);
    expect(brainStepReady({ ...gwArgs, currentKey: gwKey, tested: { ok: true, key: gwKey } })).toBe(true);
  });

  it("gateway: a passing test for OTHER inputs (stale key) does not unlock Next", () => {
    const staleKey = testKey({ ...GW, modelName: "old-model" });
    expect(brainStepReady({ ...gwArgs, currentKey: gwKey, tested: { ok: true, key: staleKey } })).toBe(false);
  });

  it("gateway: a missing endpoint or model blocks even with a passing test", () => {
    expect(brainStepReady({ ...gwArgs, apiBase: "", currentKey: gwKey, tested: { ok: true, key: gwKey } })).toBe(false);
    expect(brainStepReady({ ...gwArgs, modelName: "", currentKey: gwKey, tested: { ok: true, key: gwKey } })).toBe(false);
  });

  it("OAuth: stays blocked while signed out, even with a model and a passing test", () => {
    const key = testKey({ runtimeKind: "native", provider: "anthropic-oauth", apiBase: "", apiKey: "", modelName: "claude" });
    expect(
      brainStepReady({ runtimeKind: "native", provider: "anthropic-oauth", apiBase: "", modelName: "claude", signedIn: false, currentKey: key, tested: { ok: true, key } }),
    ).toBe(false);
  });

  it("OAuth: signed in + model + passing test unlocks (no gateway base needed); not-yet-tested still blocks", () => {
    const key = testKey({ runtimeKind: "native", provider: "openai-codex", apiBase: "", apiKey: "", modelName: "gpt" });
    expect(
      brainStepReady({ runtimeKind: "native", provider: "openai-codex", apiBase: "", modelName: "gpt", signedIn: true, currentKey: key, tested: { ok: true, key } }),
    ).toBe(true);
    expect(
      brainStepReady({ runtimeKind: "native", provider: "openai-codex", apiBase: "", modelName: "gpt", signedIn: true, currentKey: key, tested: null }),
    ).toBe(false);
  });

  it("ACP (legacy) always advances — the coding agent is the brain, no test required", () => {
    expect(
      brainStepReady({ runtimeKind: "acp", provider: "openai", apiBase: "", modelName: "", signedIn: false, currentKey: "anything", tested: null }),
    ).toBe(true);
  });
});

// Local model auto-pick (#4572): the wizard selects the first endpoint that's reachable
// AND actually has models — a server that's up but empty is skipped, and nothing usable
// returns null (→ the "start a server" hint).
describe("pickLocalEndpoint", () => {
  const ep = (over: Partial<LocalEndpoint>): LocalEndpoint => ({
    id: "x",
    label: "X",
    base_url: "http://127.0.0.1:11434/v1",
    found: false,
    models: [],
    error: "",
    ...over,
  });

  it("picks the first found endpoint that has models, skipping an earlier empty one", () => {
    const empty = ep({ id: "lmstudio", label: "LM Studio", base_url: "http://127.0.0.1:1234/v1", found: true, error: "running, but no models pulled" });
    const ready = ep({ id: "ollama", label: "Ollama", found: true, models: ["llama3.2", "qwen"] });
    expect(pickLocalEndpoint([empty, ready])).toBe(ready);
  });

  it("does not pick a server that is up but has no models pulled", () => {
    const empty = ep({ found: true, models: [], error: "running, but no models pulled" });
    expect(pickLocalEndpoint([empty])).toBeNull();
  });

  it("returns null when nothing is reachable", () => {
    const refused = ep({ id: "ollama", found: false, error: "connection refused" });
    const timedOut = ep({ id: "lmstudio", base_url: "http://127.0.0.1:1234/v1", found: false, error: "connection timed out" });
    expect(pickLocalEndpoint([refused, timedOut])).toBeNull();
    expect(pickLocalEndpoint([])).toBeNull();
  });
});

// The Local model card end-to-end (#4572): picking it detects servers, auto-fills the
// endpoint + model, and the connection test unlocks Next; with nothing running it shows a
// hint and a Re-check button and never hangs (the detection is timeout-bounded).
describe("SetupWizard — the Local model card (#4572)", () => {
  const endpointInput = () =>
    [...container.querySelectorAll<HTMLLabelElement>("label.pl-field")]
      .find((l) => l.querySelector(".pl-field__label")?.textContent === "Endpoint")
      ?.querySelector("input") ?? null;
  const modelInput = () => container.querySelector<HTMLInputElement>('input[list="model-options"]');

  it("offers a Local model card with the no-account blurb", async () => {
    await mountToBrain();
    expect(radioFor("local")).toBeDefined();
    expect(container.textContent).toContain("Use Ollama or LM Studio running on this machine. No account needed.");
  });

  it("picking it fills the endpoint + model from the detected server, and the test unlocks Next", async () => {
    vi.spyOn(api, "localModels").mockResolvedValue({
      endpoints: [
        { id: "ollama", label: "Ollama", base_url: "http://127.0.0.1:11434/v1", found: true, models: ["llama3.2", "qwen"], error: "" },
        { id: "lmstudio", label: "LM Studio", base_url: "http://127.0.0.1:1234/v1", found: false, models: [], error: "connection refused" },
      ],
    } as never);
    await mountToBrain();
    await click(radioFor("local"));
    await tick(() => modelInput()?.value === "llama3.2");
    expect(api.localModels).toHaveBeenCalled();
    expect(endpointInput()?.value).toBe("http://127.0.0.1:11434/v1");
    expect(modelInput()?.value).toBe("llama3.2");
    // The reachable server with models reads as available; the unreachable one as its error.
    expect(container.textContent).toContain("2 models available");
    expect(container.textContent).toContain("connection refused");
    // bd-jq56.1's auto-test / Next gate applies unchanged — a passing test unlocks Next.
    await click(button(/^Test connection/));
    await tick(() => !button(/^Next/)?.disabled);
    expect(button(/^Next/)?.disabled).toBe(false);
    expect(api.testModel).toHaveBeenCalledWith("http://127.0.0.1:11434/v1", "", "llama3.2", "openai");
  });

  it("with nothing running, shows the start-a-server hint and a Re-check, and holds Next", async () => {
    vi.spyOn(api, "localModels").mockResolvedValue({
      endpoints: [
        { id: "ollama", label: "Ollama", base_url: "http://127.0.0.1:11434/v1", found: false, models: [], error: "connection refused" },
        { id: "lmstudio", label: "LM Studio", base_url: "http://127.0.0.1:1234/v1", found: false, models: [], error: "connection refused" },
      ],
    } as never);
    await mountToBrain();
    await click(radioFor("local"));
    await tick(() => Boolean(container.textContent?.includes("No local model server found")));
    expect(container.textContent).toContain("No local model server found");
    expect(button(/Re-check/)).toBeDefined();
    // No model was auto-picked, so the gate still blocks (nothing to run).
    expect(modelInput()?.value).toBe("");
    expect(button(/^Next/)?.disabled).toBe(true);
  });

  it("the endpoint and model stay editable after a pick", async () => {
    vi.spyOn(api, "localModels").mockResolvedValue({
      endpoints: [{ id: "ollama", label: "Ollama", base_url: "http://127.0.0.1:11434/v1", found: true, models: ["llama3.2"], error: "" }],
    } as never);
    await mountToBrain();
    await click(radioFor("local"));
    await tick(() => modelInput()?.value === "llama3.2");
    await typeInto(endpointInput(), "http://127.0.0.1:1234/v1");
    await typeInto(modelInput(), "custom-model");
    expect(endpointInput()?.value).toBe("http://127.0.0.1:1234/v1");
    expect(modelInput()?.value).toBe("custom-model");
  });
});

describe("testKey", () => {
  const base = { provider: "openai", runtimeKind: "native" as const, apiBase: " https://gw/v1 ", apiKey: " sk-1 ", modelName: " m " };

  it("is stable for identical inputs and trims surrounding whitespace", () => {
    expect(testKey(base)).toBe(testKey({ ...base }));
    expect(testKey(base)).toBe(testKey({ ...base, apiBase: "https://gw/v1", apiKey: "sk-1", modelName: "m" }));
  });

  it("changes when any of provider / runtime / base / key / model changes", () => {
    expect(testKey(base)).not.toBe(testKey({ ...base, provider: "anthropic-oauth" }));
    expect(testKey(base)).not.toBe(testKey({ ...base, runtimeKind: "acp" }));
    expect(testKey(base)).not.toBe(testKey({ ...base, apiBase: "https://other/v1" }));
    expect(testKey(base)).not.toBe(testKey({ ...base, apiKey: "sk-2" }));
    expect(testKey(base)).not.toBe(testKey({ ...base, modelName: "m2" }));
  });
});

describe("withTimeout", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("clears its timer when the main promise resolves first", async () => {
    await expect(withTimeout(Promise.resolve("ok"), 5000, "probe")).resolves.toBe("ok");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("clears its timer when the main promise rejects first", async () => {
    await expect(withTimeout(Promise.reject(new Error("boom")), 5000, "probe")).rejects.toThrow("boom");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejects with the label once the timeout elapses", async () => {
    const pending = withTimeout(new Promise<never>(() => {}), 5000, "Model probe");
    const assertion = expect(pending).rejects.toThrow("Model probe timed out after 5s");
    await vi.advanceTimersByTimeAsync(5000);
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("wizard navigation", () => {
  it("connects a model before configuring the agent", () => {
    expect(steps).toEqual(["welcome", "brain", "agent", "finish"]);
  });

  it("requires a name, required bundle answers, and a completed peek", () => {
    expect(agentStepReady("protoagent", false, false)).toBe(true);
    expect(agentStepReady("  ", false, false)).toBe(false);
    expect(agentStepReady("engineer", true, false)).toBe(false);
    expect(agentStepReady("engineer", false, true)).toBe(false);
  });

  it("filters the initial catalog but keeps the current choice visible", () => {
    expect(primaryArchetypes(ARCHETYPES, ["basic"], "basic").map((a) => a.id)).toEqual(["basic"]);
    expect(primaryArchetypes(ARCHETYPES, undefined, "basic")).toEqual(ARCHETYPES);
    expect(primaryArchetypes(ARCHETYPES, ["basic"], "engineer")).toEqual(ARCHETYPES);
  });

  it("refreshes OAuth and auto-probes on entering the model step", async () => {
    await mountToBrain();
    expect(api.oauthStatus).toHaveBeenCalled();
    expect(api.models).toHaveBeenCalledWith("https://gw/v1", "", "openai");
    expect(button(/^Next/)?.disabled).toBe(true);
    await click(button(/^Test connection/));
    expect(button(/^Next/)?.disabled).toBe(false);
    const calls = vi.mocked(api.oauthStatus).mock.calls.length;
    await click(button(/^Next/));
    await click(button(/^Back/));
    expect(vi.mocked(api.oauthStatus).mock.calls.length).toBeGreaterThan(calls);
  });
});
