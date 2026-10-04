// NewAgentPanel — the two-step create-from-archetype flow (lib/archetypeFlow):
//   1. PICK: the archetype cards only (shared ArchetypePicker) + Next. No name, no config.
//   2. SET UP: a DS Dialog (shared ArchetypeSetupForm) — the name first, pre-filled with the
//      archetype's suggested name; the bundle's config_inputs as real fields (short label +
//      help line, folder picker for `path`, labelled switch for booleans); Advanced
//      collapsed; Back (keeps choices) + Create.
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts`, so we build elements with React.createElement rather than JSX). The DS
// Dialog portals to <body>, so step-2 queries go through `document`, not `container`.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewAgentPanel } from "../NewAgentPanel";
import { api } from "../../lib/api";
import { HARD_GATE_HINT, SETUP_OPTIONAL_HELP } from "../../lib/pickerCopy";
import { PREVIEW_ARCHETYPES_KEY, setShowPreviewArchetypes } from "../../lib/previewArchetypesPref";
import type { Archetype, ArchetypeFromUrl, ArchetypePreview, PythonRuntimePayload } from "../../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ARCHETYPES: Archetype[] = [
  { id: "basic", label: "Basic", icon: "bot", blurb: "A plain agent", bundle: null, soul: "" },
  { id: "scout", label: "Scout", icon: "search", blurb: "Research bundle", bundle: "https://example.com/scout.git", soul: "persona" },
];

// A bundle whose peek declares config_inputs (a path with help, a boolean with help, a
// required string) and one MCP input — the Engineer-style case the redesign is for.
const ENGINEER: Archetype = {
  id: "engineer",
  label: "Engineer",
  icon: "wrench",
  blurb: "Ships code",
  bundle: "https://example.com/engineer.git",
  soul: "# Engineer",
  requires_tools: ["github_create_issue"],
};
const ENGINEER_PREVIEW: ArchetypePreview = {
  id: "engineer",
  bundle: {
    kind: "bundle",
    name: "Engineer",
    members: [],
    mcp: [{ id: "gh", name: "GitHub", template: {}, inputs: [{ key: "github_token", label: "GitHub token", secret: true }] }],
    secrets: [],
    config_inputs: [
      { key: "engineer.repo", label: "Start in a local repo", type: "path", help: "It is registered as a project and the terminal opens there." },
      { key: "github.write", label: "Allow GitHub writes", type: "boolean", default: false, help: "Off = read-only." },
    ],
  },
};

// Managed-runtime payloads for the choose-time warning (#2186 follow-on).
const runtimePayload = (over: Partial<PythonRuntimePayload["python"]>): PythonRuntimePayload => ({
  python: {
    needed: true,
    managed: true,
    managed_version: "3.12.13",
    exe: "/x/python",
    baseline_installed: true,
    baseline_current: true,
    supported: true,
    target_version: "3.12.13",
    ...over,
  },
  install: { state: "idle", pct: 0, message: "", error: null },
});

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  try {
    localStorage.removeItem(PREVIEW_ARCHETYPES_KEY); // the opt-in is per console — start every test OFF
  } catch {
    /* no storage */
  }
  vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: ARCHETYPES });
  vi.spyOn(api, "pythonRuntime").mockResolvedValue(runtimePayload({})); // provisioned by default
  vi.spyOn(api, "archetypePreview").mockImplementation(async (id: string) =>
    id === "engineer" ? ENGINEER_PREVIEW : { id, bundle: null },
  );
  vi.spyOn(api, "fleet").mockResolvedValue({ agents: [] });
  vi.spyOn(api, "delegates").mockResolvedValue({ delegates: [] });
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

async function mountPanel(props: Parameters<typeof NewAgentPanel>[0] = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(h(QueryClientProvider, { client: qc }, h(ToastProvider, null, h(NewAgentPanel, props))));
  });
  // react-query commits the resolved archetypes on a follow-up tick — wait for the cards.
  await tick(() => Boolean(container.querySelector(".pl-radiocard")));
  if (!container.querySelector(".pl-radiocard")) throw new Error("archetypes never rendered — the mocked query did not commit");
}

const buttons = (root: ParentNode = document) => [...root.querySelectorAll<HTMLButtonElement>("button")];
const buttonNamed = (re: RegExp, root: ParentNode = document) => buttons(root).find((b) => re.test(b.textContent?.trim() ?? ""));
const dialog = () => document.querySelector<HTMLElement>(".archetype-setup-dialog");
const nameInput = () => dialog()?.querySelector<HTMLInputElement>('input[aria-label="Agent name"]') ?? null;
const radioFor = (value: string) =>
  [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find((r) => r.value === value);

async function click(el: HTMLElement | undefined | null) {
  if (!el) throw new Error("nothing to click");
  await act(async () => {
    el.click();
  });
}
async function pick(value: string) {
  await click(radioFor(value));
}
async function next() {
  await click(buttonNamed(/^Next/, container));
  await tick(() => Boolean(dialog()));
}
async function typeInto(input: HTMLInputElement | HTMLTextAreaElement | null, value: string) {
  if (!input) throw new Error("no input");
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const createButton = () => buttonNamed(/^Create/, dialog() ?? document);
function mockCreate() {
  return vi.spyOn(api, "createAgent").mockResolvedValue({ ok: true, agent: { id: "x-0", name: "x" } as never, installed: [] });
}

describe("NewAgentPanel — step 1: the picker is cards only", () => {
  it("offers all three new-agent sources, archetype first (#2106)", async () => {
    await mountPanel();
    const tabs = [...container.querySelectorAll<HTMLElement>('[role="tab"]')];
    expect(tabs.map((t) => t.textContent)).toEqual(["From an archetype", "From a bundle URL", "From a snapshot"]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
  });

  it("renders no name field and no config on the picker — just cards and Next", async () => {
    await mountPanel();
    expect(document.querySelector('input[aria-label="Agent name"]')).toBeNull();
    expect(dialog()).toBeNull();
    expect(radioFor("basic")).toBeTruthy();
    expect(radioFor("scout")).toBeTruthy();
    expect(buttonNamed(/^Next/, container)).toBeTruthy();
    expect(buttonNamed(/^Create/, container)).toBeUndefined();
  });

  it("gives every card its own 'What's included' DS Button", async () => {
    await mountPanel();
    const links = [...container.querySelectorAll('.archetype-card button[aria-label^="What\'s included"]')];
    expect(links.every((l) => l.classList.contains("pl-btn"))).toBe(true);
    expect(links.map((l) => l.getAttribute("aria-label"))).toEqual(["What's included in Basic", "What's included in Scout"]);
  });

  it("wraps the card list in a bounded scroll container (#2193) with Next below it", async () => {
    await mountPanel();
    const scroll = container.querySelector<HTMLElement>(".archetype-card-scroll");
    expect(scroll!.style.overflowY).toBe("auto");
    expect(scroll!.style.maxHeight).not.toBe("");
    expect(scroll!.compareDocumentPosition(buttonNamed(/^Next/, container)!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("NewAgentPanel — step 2: set up in a dialog", () => {
  it("Next opens the set-up dialog with the name pre-filled from the archetype", async () => {
    await mountPanel();
    await pick("scout");
    await next();
    expect(dialog()?.textContent).toContain("Set up Scout");
    expect(nameInput()?.value).toBe("scout");
    expect(createButton()?.textContent).toContain("Create from Scout");
  });

  it("the suggested name steps around a name already on the fleet", async () => {
    vi.spyOn(api, "fleet").mockResolvedValue({ agents: [{ name: "scout", id: "scout-1", port: 1, pid: null, running: false, bundle: "" }] });
    await mountPanel();
    await tick(() => false); // let the fleet query commit
    await pick("scout");
    await next();
    expect(nameInput()?.value).toBe("scout-2");
  });

  it("Back returns to the picker keeping the typed name and answers", async () => {
    await mountPanel();
    await pick("scout");
    await next();
    await typeInto(nameInput(), "scouty");
    await click(buttonNamed(/^Back/, dialog()!));
    expect(dialog()).toBeNull();
    expect(radioFor("scout")?.checked).toBe(true);
    await next();
    expect(nameInput()?.value).toBe("scouty");
  });

  it("an invalid name disables Create and says why", async () => {
    await mountPanel();
    await next();
    await typeInto(nameInput(), "bad name!");
    expect(createButton()?.disabled).toBe(true);
    expect(dialog()?.textContent).toContain("Use only letters, numbers, dashes and underscores.");
  });

  it("renders config_inputs as proper fields: short label, help line, folder picker, labelled switch", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, ENGINEER] });
    await mountPanel();
    await pick("engineer");
    await next();
    await tick(() => Boolean(dialog()?.textContent?.includes("Start in a local repo")));
    const d = dialog()!;
    // The optional note is said ONCE, above the group.
    expect(d.textContent?.split(SETUP_OPTIONAL_HELP).length).toBe(2);
    // path → the Settings folder picker (input + Browse…), with its help line.
    expect(d.querySelector('.path-picker input[aria-label="Start in a local repo"]')).not.toBeNull();
    expect(buttonNamed(/Browse/, d)).toBeTruthy();
    expect(d.textContent).toContain("It is registered as a project and the terminal opens there.");
    // boolean → a DS switch carrying its short label, the help described-by.
    const sw = d.querySelector<HTMLInputElement>(".pl-switch input");
    expect(sw?.closest(".pl-switch")?.textContent).toContain("Allow GitHub writes");
    expect(document.getElementById(sw!.getAttribute("aria-describedby")!)?.textContent).toBe("Off = read-only.");
    // Advanced is collapsed: the MCP input isn't on screen until it's opened.
    expect(d.querySelector('input[aria-label="GitHub token"]')).toBeNull();
    await click(buttonNamed(/^Advanced/, d));
    expect(d.querySelector('input[aria-label="GitHub token"]')).not.toBeNull();
  });

  it("Create sends the name, the config answers, the advanced values and the contract", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, ENGINEER] });
    const create = mockCreate();
    await mountPanel();
    await pick("engineer");
    await next();
    await tick(() => Boolean(dialog()?.querySelector(".path-picker input")));
    const d = dialog()!;
    await typeInto(d.querySelector<HTMLInputElement>(".path-picker input"), "/src/app");
    await click(d.querySelector<HTMLInputElement>(".pl-switch input"));
    await click(buttonNamed(/^Advanced/, d));
    await typeInto(d.querySelector<HTMLInputElement>('input[aria-label="GitHub token"]'), "ghp_x");
    await click(createButton());

    expect(create).toHaveBeenCalledWith({
      name: "engineer",
      bundle: ENGINEER.bundle,
      soul: "# Engineer",
      inputs: { github_token: "ghp_x" },
      secrets: undefined,
      config_inputs: { "engineer.repo": "/src/app", "github.write": true },
      requires_tools: ["github_create_issue"],
    });
  });

  it("a required config answer left blank keeps Create disabled with the hard-gate hint", async () => {
    const required: ArchetypePreview = {
      id: "engineer",
      bundle: { ...ENGINEER_PREVIEW.bundle!, config_inputs: [{ key: "engineer.repo", label: "Repo", type: "string", required: true }] },
    };
    vi.spyOn(api, "archetypePreview").mockResolvedValue(required);
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [ENGINEER] });
    await mountPanel();
    await next();
    await tick(() => Boolean(dialog()?.textContent?.includes(HARD_GATE_HINT)));
    expect(createButton()?.disabled).toBe(true);
    await typeInto(dialog()!.querySelector<HTMLInputElement>('input[aria-label="Repo"]'), "x");
    expect(createButton()?.disabled).toBe(false);
  });

  it("holds Create (button and Enter) while the bundle's peek is loading (#3632)", async () => {
    // Until the peek lands the bundle's required config_inputs are unknown, so the
    // missing-answer gate can't fire — Create must wait rather than post past them.
    let resolvePeek: (p: ArchetypePreview) => void = () => {};
    vi.spyOn(api, "archetypePreview").mockImplementation(
      () => new Promise<ArchetypePreview>((r) => (resolvePeek = r)),
    );
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [ENGINEER] });
    const create = mockCreate();
    await mountPanel();
    await next();
    expect(createButton()?.disabled).toBe(true);
    await act(async () => {
      nameInput()!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(create).not.toHaveBeenCalled();

    await act(async () => resolvePeek(ENGINEER_PREVIEW));
    await tick(() => Boolean(dialog()?.textContent?.includes("Start in a local repo")));
    expect(createButton()?.disabled).toBe(false);
  });

  it("a FAILED peek keeps Create (button and Enter) held and offers a Retry that refetches (#3632)", async () => {
    // Not loading ≠ questions known: after a failed fetch there's no data, so the
    // required-answer gate can't fire — Create must stay held until a retry lands.
    const peek = vi.spyOn(api, "archetypePreview").mockRejectedValue(new Error("peek exploded"));
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [ENGINEER] });
    const create = mockCreate();
    await mountPanel();
    await next();
    // The panel's query retries once (~1s) before it settles into the error state.
    for (let i = 0; i < 40 && !dialog()?.querySelector('[role="alert"]'); i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 100));
      });
    }
    const alert = dialog()!.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain("peek exploded");
    expect(createButton()?.disabled).toBe(true);
    await act(async () => {
      nameInput()!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(create).not.toHaveBeenCalled();

    const calls = peek.mock.calls.length;
    peek.mockResolvedValue(ENGINEER_PREVIEW);
    await click(buttonNamed(/^Retry$/, dialog()!));
    await tick(() => Boolean(dialog()?.textContent?.includes("Start in a local repo")));
    expect(peek.mock.calls.length).toBe(calls + 1);
    expect(dialog()!.querySelector('[role="alert"]')).toBeNull();
    expect(createButton()?.disabled).toBe(false);
  }, 10_000);

  it("announces the help line on path and text fields (aria-describedby, #3632)", async () => {
    const withHelp: ArchetypePreview = {
      id: "engineer",
      bundle: {
        ...ENGINEER_PREVIEW.bundle!,
        config_inputs: [
          ...ENGINEER_PREVIEW.bundle!.config_inputs!,
          { key: "engineer.branch", label: "Branch", type: "string", help: "Checked out on first run." },
        ],
      },
    };
    vi.spyOn(api, "archetypePreview").mockResolvedValue(withHelp);
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [ENGINEER] });
    await mountPanel();
    await next();
    await tick(() => Boolean(dialog()?.querySelector(".path-picker input")));
    const d = dialog()!;
    const describedText = (el: Element | null) =>
      document.getElementById(el?.getAttribute("aria-describedby") ?? "")?.textContent;
    expect(describedText(d.querySelector('.path-picker input[aria-label="Start in a local repo"]'))).toBe(
      "It is registered as a project and the terminal opens there.",
    );
    expect(describedText(d.querySelector('input[aria-label="Branch"]'))).toBe("Checked out on first run.");
  });

  it("omits requires_tools for a contract-less archetype", async () => {
    const create = mockCreate();
    await mountPanel();
    await next(); // default card = Basic
    await typeInto(nameInput(), "plain");
    await click(createButton());
    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0][0].requires_tools).toBeUndefined();
    expect(create.mock.calls[0][0].bundle).toBeNull();
  });
});

describe("NewAgentPanel — choose-time runtime warning (#2186 follow-on)", () => {
  const DOCSY: Archetype = {
    id: "docsy",
    label: "Docsy",
    icon: "briefcase",
    blurb: "Documents",
    bundle: "https://example.com/docsy.git",
    soul: "d",
    requires: ["python_runtime"],
  };

  it("warns when the picked archetype requires the runtime and it isn't provisioned", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, DOCSY] });
    vi.spyOn(api, "pythonRuntime").mockResolvedValue(runtimePayload({ managed: false, baseline_current: false }));
    await mountPanel();
    await pick("docsy");
    const note = container.querySelector(".archetype-runtime-notice");
    expect(note!.textContent).toContain("Docsy needs the managed Python runtime");
    expect(note!.textContent).toContain("Settings ▸ Tools");
  });

  it("stays silent when the runtime is provisioned", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, DOCSY] });
    await mountPanel();
    await pick("docsy");
    expect(container.querySelector(".archetype-runtime-notice")).toBeNull();
  });
});

describe("NewAgentPanel — advanced-tier archetypes collapse behind a toggle", () => {
  const WITH_ADVANCED: Archetype[] = [
    { id: "basic", label: "Basic", icon: "bot", blurb: "A plain agent", bundle: null, soul: "" },
    { id: "pm", label: "Project Manager", icon: "clipboard", blurb: "PM bundle", bundle: "https://example.com/pm.git", soul: "pm", tier: "advanced" },
  ];
  const advancedToggle = () => buttonNamed(/^Advanced \(/, container);

  it("files advanced cards under a collapsed 'Advanced (N)' toggle below the standard ones", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_ADVANCED });
    await mountPanel();
    expect(radioFor("pm")).toBeUndefined();
    expect(advancedToggle()?.textContent).toContain("Advanced (1)");
    expect(advancedToggle()?.getAttribute("aria-expanded")).toBe("false");
    await click(advancedToggle());
    expect(radioFor("pm")).toBeTruthy();
  });

  it("picking an advanced card drives the same set-up step", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_ADVANCED });
    await mountPanel();
    await click(advancedToggle());
    await pick("pm");
    await next();
    expect(dialog()?.textContent).toContain("Set up Project Manager");
    expect(nameInput()?.value).toBe("project-manager");
  });

  it("with every archetype standard, Advanced carries no count — it only holds the preview switch", async () => {
    await mountPanel();
    expect(advancedToggle()).toBeUndefined();
    const toggle = buttonNamed(/^Advanced$/, container);
    expect(toggle).toBeTruthy();
    await click(toggle);
    expect(container.textContent).toContain("Show preview archetypes");
    expect(container.querySelectorAll('input[type="radio"]').length).toBe(2); // no extra cards
  });
});

describe("NewAgentPanel — onDone hands over the created agent's name AND id", () => {
  it("calls onDone with both the name and the id on a successful create", async () => {
    const onDone = vi.fn();
    vi.spyOn(api, "createAgent").mockResolvedValue({ ok: true, agent: { id: "newbot-ab12", name: "newbot" } as never, installed: [] });
    await mountPanel({ onDone });
    await next();
    await typeInto(nameInput(), "newbot");
    await click(createButton());
    await tick(() => onDone.mock.calls.length > 0);
    expect(onDone).toHaveBeenCalledWith("newbot", "newbot-ab12");
  });

  it("survives a success response with no agent record: name lands, id is undefined", async () => {
    const onDone = vi.fn();
    vi.spyOn(api, "createAgent").mockResolvedValue({ ok: true } as never);
    await mountPanel({ onDone });
    await next();
    await typeInto(nameInput(), "ghostbot");
    await click(createButton());
    await tick(() => onDone.mock.calls.length > 0);
    expect(onDone).toHaveBeenCalledWith("ghostbot", undefined);
  });

  it("does not call onDone when the create fails — the error toast shows instead", async () => {
    const onDone = vi.fn();
    vi.spyOn(api, "createAgent").mockRejectedValue(new Error("bundle clone failed"));
    await mountPanel({ onDone });
    await next();
    await typeInto(nameInput(), "failbot");
    await click(createButton());
    await tick(() => document.querySelector(".pl-toast") !== null);
    expect(document.querySelector(".pl-toast")?.textContent).toContain("Couldn't create agent");
    expect(onDone).not.toHaveBeenCalled();
  });
});

// ── Held (preview) archetypes: opt-in, per console, badged ─────────────────────────────

describe("NewAgentPanel — preview archetypes are opt-in under Advanced", () => {
  const HELD: Archetype = {
    id: "analyst",
    label: "Analyst",
    icon: "chart",
    blurb: "Answers from your data",
    bundle: "https://github.com/protoLabsAI/analyst-archetype",
    soul: "# Analyst",
    held: true,
  };
  const previewSwitch = () =>
    [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].find((i) =>
      i.closest("label")?.textContent?.includes("Show preview archetypes"),
    );

  afterEach(() => act(() => setShowPreviewArchetypes(false)));

  it("never asks for held archetypes by default, and never renders one", async () => {
    const spy = vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, HELD] });
    await mountPanel();
    expect(spy.mock.calls.every((c) => !c[0])).toBe(true); // no include_held on the default fetch
    await click(buttonNamed(/^Advanced/, container));
    expect(radioFor("analyst")).toBeUndefined(); // even if a server sent one, it stays hidden
    expect(previewSwitch()?.checked).toBe(false);
  });

  it("the switch refetches with include_held, badges the held card 'Preview', and persists", async () => {
    const spy = vi
      .spyOn(api, "archetypes")
      .mockImplementation(async (includeHeld?: boolean) => ({ archetypes: includeHeld ? [...ARCHETYPES, HELD] : ARCHETYPES }));
    await mountPanel();
    await click(buttonNamed(/^Advanced/, container));
    await click(previewSwitch());
    await tick(() => Boolean(radioFor("analyst")));
    expect(spy).toHaveBeenCalledWith(true);
    const card = radioFor("analyst")!.closest(".pl-radiocard")!;
    expect(card.querySelector(".pl-badge")?.textContent).toBe("Preview");
    expect(localStorage.getItem(PREVIEW_ARCHETYPES_KEY)).toBe("1");
    // A held card drives the same set-up step as any other.
    await pick("analyst");
    await next();
    expect(dialog()?.textContent).toContain("Set up Analyst");
  });

  it("a console that opted in before opens with the preview cards already there", async () => {
    setShowPreviewArchetypes(true);
    vi.spyOn(api, "archetypes").mockImplementation(async (includeHeld?: boolean) => ({
      archetypes: includeHeld ? [...ARCHETYPES, HELD] : ARCHETYPES,
    }));
    await mountPanel();
    await click(buttonNamed(/^Advanced \(1\)/, container));
    expect(radioFor("analyst")).toBeTruthy();
  });
});

// ── From a bundle URL: entry → preview + trust → the same set-up dialog → create ─────────

describe("NewAgentPanel — From a bundle URL", () => {
  const FOUND: ArchetypeFromUrl = {
    id: "analyst-archetype",
    archetype: {
      id: "analyst-archetype",
      label: "Analyst",
      icon: "chart",
      blurb: "Answers questions from your data files.",
      bundle: "https://github.com/acme/analyst-archetype",
      soul: "# Analyst",
      ref: "v0.1.0",
      requires_tools: ["data_query"],
    },
    bundle: {
      kind: "bundle",
      id: "analyst-archetype",
      name: "Analyst",
      description: "The analyst bundle.",
      members: [
        { id: "data", builtin: false, ref: "v0.1.0", name: "Data", version: "0.1.0" },
        { id: "notes", builtin: true, name: "Notes" },
      ],
      mcp: [],
      secrets: [],
      config_inputs: [{ key: "data.data_dirs", label: "Data folders", type: "string", required: true }],
    },
    trusted: false,
    source: "github.com/acme/analyst-archetype",
  };
  const urlInput = () => container.querySelector<HTMLInputElement>('input[type="url"]');
  const refInput = () => container.querySelector<HTMLInputElement>('input[placeholder="v0.1.0"]');
  const trustBox = () =>
    [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].find((i) =>
      i.closest("label")?.textContent?.includes("I trust this repository"),
    );
  async function openUrlTab() {
    await mountPanel();
    await click([...container.querySelectorAll<HTMLElement>('[role="tab"]')].find((t) => t.textContent === "From a bundle URL"));
  }
  async function lookUp() {
    await click(buttonNamed(/Look up/, container));
    await tick(() => Boolean(container.querySelector(".bundle-url-preview")));
  }

  it("rejects a non-git URL on the client — nothing is fetched", async () => {
    const spy = vi.spyOn(api, "archetypeFromUrl");
    await openUrlTab();
    await typeInto(urlInput(), "not a repo");
    await click(buttonNamed(/Look up/, container));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("isn't a git repository URL");
    expect(spy).not.toHaveBeenCalled();
  });

  it("folds a GitHub /tree/<ref> URL, previews what it installs, and gates Next on trust", async () => {
    const spy = vi.spyOn(api, "archetypeFromUrl").mockResolvedValue(FOUND);
    await openUrlTab();
    await typeInto(urlInput(), "https://github.com/acme/analyst-archetype/tree/v0.1.0");
    await lookUp();
    expect(spy).toHaveBeenCalledWith("https://github.com/acme/analyst-archetype", "v0.1.0");
    const preview = container.querySelector<HTMLElement>(".bundle-url-preview")!;
    expect(preview.textContent).toContain("github.com/acme/analyst-archetype@v0.1.0");
    expect(preview.textContent).toContain("The analyst bundle.");
    expect(preview.textContent).toContain("v0.1.0"); // the member's pinned ref
    expect(preview.textContent).toContain("built-in"); // the builtin it turns on
    expect(preview.textContent).toContain("It will ask for: Data folders");
    expect(preview.textContent).toContain("isn't an official source");
    const nextBtn = buttonNamed(/^Next/, container)!;
    expect(nextBtn.disabled).toBe(true);
    await click(trustBox());
    expect(nextBtn.disabled).toBe(false);
  });

  it("a trusted source needs no extra ack; editing the URL drops the preview", async () => {
    vi.spyOn(api, "archetypeFromUrl").mockResolvedValue({ ...FOUND, trusted: true });
    await openUrlTab();
    await typeInto(urlInput(), "https://github.com/acme/analyst-archetype");
    await lookUp();
    expect(trustBox()).toBeUndefined();
    expect(buttonNamed(/^Next/, container)!.disabled).toBe(false);
    await typeInto(urlInput(), "https://github.com/acme/other");
    expect(container.querySelector(".bundle-url-preview")).toBeNull();
    expect(buttonNamed(/^Next/, container)!.disabled).toBe(true);
  });

  it("Next runs the same set-up dialog and Create posts bundle + ref + answers", async () => {
    vi.spyOn(api, "archetypeFromUrl").mockResolvedValue(FOUND);
    const create = mockCreate();
    await openUrlTab();
    await typeInto(urlInput(), "https://github.com/acme/analyst-archetype");
    await typeInto(refInput(), "v0.1.0");
    await lookUp();
    await click(trustBox());
    await next();
    expect(dialog()?.textContent).toContain("Set up Analyst");
    expect(dialog()?.textContent).toContain("installs 2 plugins");
    expect(nameInput()?.value).toBe("analyst");
    // The required config_inputs answer hard-gates Create, same as a catalog card.
    expect(createButton()?.disabled).toBe(true);
    const field = [...dialog()!.querySelectorAll<HTMLInputElement>("input")].find((i) =>
      i.closest("label")?.textContent?.includes("Data folders"),
    );
    await typeInto(field ?? null, "/data");
    expect(createButton()?.disabled).toBe(false);
    await click(createButton());
    await tick(() => create.mock.calls.length > 0);
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "analyst",
        bundle: "https://github.com/acme/analyst-archetype",
        ref: "v0.1.0",
        soul: "# Analyst",
        config_inputs: { "data.data_dirs": "/data" },
        requires_tools: ["data_query"],
      }),
    );
  });
});
