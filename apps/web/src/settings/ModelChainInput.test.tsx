// ADR 0133 M7b — the one chain control every model role uses (ModelChainInput). Covered here:
// the ordered rows (criterion 1), "+ Add fallback" + drop-empty (2), reorder + focus + live region
// (3), the "Inherits <parent>" toggletip / Set-its-own-chain / Use-parent (4), the main chain's
// legacy fold + undeletable last row (5), the unknown-model warning (6), the phone card layout (8),
// and the no-ellipsis / wrapping-id CSS (7, 9). These all fail on main, where model_chain still
// renders the generic combobox list.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import settingsCss from "./settings.css?raw";
import { ModelChainInput, foldLegacyFallbacks, splitModelId } from "./ModelChainInput";
import type { SettingsField } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const field = (overrides: Partial<SettingsField> = {}): SettingsField => ({
  key: "model.name",
  label: "Main",
  type: "model_chain",
  section: "Model roles",
  options: ["one", "two", "three"],
  restart: false,
  scope: "agent",
  source: "agent",
  role: "main",
  subhead: "Main and aux",
  inherits: "",
  ...overrides,
});

let host: HTMLDivElement;
let root: Root;

function stubMatchMedia(phone: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: phone && query.includes("max-width: 767px"),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
  Element.prototype.hasPointerCapture = Element.prototype.hasPointerCapture ?? (() => false);
  stubMatchMedia(false); // desktop by default
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function render(node: React.ReactNode) {
  await act(async () => root.render(node));
}
const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector<T>(sel);
const qa = <T extends Element = HTMLElement>(sel: string) => [...document.querySelectorAll<T>(sel)];
const byLabel = (label: string) => q<HTMLButtonElement>(`[aria-label="${label}"]`);
async function click(el: Element | null | undefined) {
  expect(el, "expected an element to click").toBeTruthy();
  await act(async () => (el as HTMLElement).click());
}
const text = (el: Element | null) => el?.textContent ?? "";

// ── criterion 1 ────────────────────────────────────────────────────────────────────────────────
describe("the chain rows (criterion 1)", () => {
  it("renders one Combobox per model labelled Primary then then, with ↑ ↓ ✕ a11y names", async () => {
    await render(<ModelChainInput field={field()} value={["one", "two"]} onChange={vi.fn()} />);

    const group = q('[role="group"]');
    expect(group?.getAttribute("aria-label")).toBe("Main model chain");

    const positions = qa(".model-chain__position").map((p) => p.textContent);
    expect(positions).toEqual(["Primary", "then"]);

    const inputs = qa<HTMLInputElement>(".model-chain__combobox");
    expect(inputs.map((i) => i.getAttribute("aria-label"))).toEqual([
      "Main model 1 of 2",
      "Main model 2 of 2",
    ]);
    expect(inputs.map((i) => i.value)).toEqual(["one", "two"]);

    // Accessible names from §3, and the boundary buttons disabled (not hidden).
    expect(byLabel("Move one up")!.disabled).toBe(true); // ↑ on the first row
    expect(byLabel("Move one down")!.disabled).toBe(false);
    expect(byLabel("Move two down")!.disabled).toBe(true); // ↓ on the last row
    expect(byLabel("Move two up")!.disabled).toBe(false);
    expect(byLabel("Remove one")).not.toBeNull();
    expect(byLabel("Remove two")).not.toBeNull();
  });
});

// ── criterion 2 ────────────────────────────────────────────────────────────────────────────────
describe("+ Add fallback (criterion 2)", () => {
  it("appends an empty row, focuses it, and never saves the empty row", async () => {
    const onChange = vi.fn();
    await render(<ModelChainInput field={field()} value={["one", "two"]} onChange={onChange} />);

    const add = [...document.querySelectorAll("button")].find((b) => text(b).includes("Add fallback"));
    await click(add);

    // A third (empty) row appeared and is focused…
    const inputs = qa<HTMLInputElement>(".model-chain__combobox");
    expect(inputs).toHaveLength(3);
    expect(inputs[2].value).toBe("");
    expect(document.activeElement).toBe(inputs[2]);
    // …and adding it emitted nothing (an empty row is not a saved value).
    expect(onChange).not.toHaveBeenCalled();

    // Typing into another row emits the chain WITHOUT the still-empty trailing row.
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
      setter.call(inputs[0], "three");
      inputs[0].dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(onChange).toHaveBeenLastCalledWith(["three", "two"]);
  });
});

// ── criterion 3 ────────────────────────────────────────────────────────────────────────────────
describe("reorder (criterion 3)", () => {
  it("uses moveListItem, keeps focus on the moved row's button, and announces the new position", async () => {
    const onChange = vi.fn();
    await render(<ModelChainInput field={field()} value={["one", "two", "three"]} onChange={onChange} />);

    await click(byLabel("Move one down"));

    expect(onChange).toHaveBeenLastCalledWith(["two", "one", "three"]); // moveListItem result
    // "one" is now at position 2; focus stayed on its ↓ button, and the live region announced it.
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Move one down");
    expect(text(q(".model-chain__live"))).toBe("one moved to position 2");
  });
});

// ── criterion 4 ────────────────────────────────────────────────────────────────────────────────
describe("a blank inheriting role (criterion 4)", () => {
  const compaction = () =>
    field({ key: "compaction.model", label: "Compaction", role: "compaction", inherits: "aux" });

  it("renders only Inherits <parent> with no model id, and a toggletip listing the parent chain", async () => {
    await render(
      <ModelChainInput
        field={compaction()}
        value={[]}
        onChange={vi.fn()}
        parentChain={["one", "two"]}
      />,
    );

    const trigger = q<HTMLButtonElement>(".model-chain__inherits")!;
    expect(text(trigger)).toBe("Inherits aux");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.getAttribute("aria-controls")).toBeTruthy();
    // No model id is in the row, and no combobox renders.
    expect(q(".model-chain__combobox")).toBeNull();
    expect(text(q(".model-chain--inherited"))).not.toContain("one");

    // Opens on click, exposing the full resolved parent chain.
    await click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const dialog = q('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(qa(".model-chain__popover-list .model-chain__id").map((li) => li.textContent)).toEqual([
      "one",
      "two",
    ]);

    // Escape closes and returns focus to the trigger.
    await act(async () =>
      dialog!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
    );
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
  });

  it("Set its own chain is a ghost sm button in tab order and pre-fills the parent chain", async () => {
    const onChange = vi.fn();
    await render(
      <ModelChainInput field={compaction()} value={[]} onChange={onChange} parentChain={["one", "two"]} />,
    );

    const setOwn = q<HTMLButtonElement>(".model-chain__set-own")!;
    expect(setOwn.className).toContain("pl-btn--ghost");
    expect(setOwn.className).toContain("pl-btn--sm");
    expect(setOwn.tabIndex).toBeGreaterThanOrEqual(0); // stays in tab order (never tabIndex -1)

    await click(setOwn);
    expect(onChange).toHaveBeenCalledWith(["one", "two"]); // pre-filled with the parent chain
    expect(qa<HTMLInputElement>(".model-chain__combobox").map((i) => i.value)).toEqual(["one", "two"]);
  });

  it("Use <parent>'s chain clears an own chain back to inheriting", async () => {
    const onChange = vi.fn();
    await render(
      <ModelChainInput field={compaction()} value={["one"]} onChange={onChange} parentChain={["two"]} />,
    );

    const useParent = [...document.querySelectorAll("button")].find((b) => text(b).includes("Use aux"));
    await click(useParent);
    expect(onChange).toHaveBeenCalledWith([]);
  });
});

// ── criterion 5 ────────────────────────────────────────────────────────────────────────────────
describe("the main chain (criterion 5)", () => {
  it("disables ✕ on the last remaining row so the main chain can't be emptied", async () => {
    await render(<ModelChainInput field={field()} value={["one"]} onChange={vi.fn()} />);
    expect(byLabel("Remove one")!.disabled).toBe(true);
  });

  it("keeps ✕ enabled while more than one model remains", async () => {
    await render(<ModelChainInput field={field()} value={["one", "two"]} onChange={vi.fn()} />);
    expect(byLabel("Remove one")!.disabled).toBe(false);
    expect(byLabel("Remove two")!.disabled).toBe(false);
  });

  it("folds legacy routing.fallback_models in after the primary, and the next save writes them into model.name", async () => {
    const onChange = vi.fn();
    await render(
      <ModelChainInput field={field()} value={["one"]} onChange={onChange} legacyFallbacks={["two"]} />,
    );
    // The legacy fallback shows as a row after the primary…
    expect(qa<HTMLInputElement>(".model-chain__combobox").map((i) => i.value)).toEqual(["one", "two"]);
    // …and the next edit folds the whole chain into model.name.
    await click(byLabel("Move two up"));
    expect(onChange).toHaveBeenLastCalledWith(["two", "one"]);
  });
});

// ── criterion 6 ────────────────────────────────────────────────────────────────────────────────
describe("an unknown model (criterion 6)", () => {
  it("shows the warning under an entry that is not in field.options and keeps the entry", async () => {
    await render(<ModelChainInput field={field()} value={["one", "mystery-model"]} onChange={vi.fn()} />);

    const warnings = qa(".model-chain__warning");
    expect(warnings).toHaveLength(1); // only the unknown row warns
    expect(text(warnings[0])).toContain("Not a model on any of your connections");
    // The entry is kept, not dropped.
    expect(qa<HTMLInputElement>(".model-chain__combobox").map((i) => i.value)).toEqual([
      "one",
      "mystery-model",
    ]);
  });
});

// ── criterion 8 ────────────────────────────────────────────────────────────────────────────────
describe("the phone card layout (criterion 8)", () => {
  beforeEach(() => stubMatchMedia(true)); // below 768px

  it("renders each row as a card: prefix + name on their own wrapping lines, controls underneath", async () => {
    await render(
      <ModelChainInput field={field()} value={["anthropic-oauth:claude-haiku-5-5"]} onChange={vi.fn()} />,
    );

    expect(q(".model-chain--phone")).not.toBeNull();
    // The id is split into a muted prefix line and a name line — no inline combobox until Change.
    expect(text(q(".model-chain__id-prefix"))).toBe("anthropic-oauth:");
    expect(text(q(".model-chain__id-name"))).toBe("claude-haiku-5-5");
    expect(q(".model-chain__combobox")).toBeNull();

    // Change opens a full-width Combobox with Cancel and Done.
    const change = [...document.querySelectorAll("button")].find((b) => text(b).includes("Change"));
    await click(change);
    expect(q(".model-chain__combobox")).not.toBeNull();
    const buttons = [...document.querySelectorAll("button")].map(text);
    expect(buttons.some((t) => t.includes("Cancel"))).toBe(true);
    expect(buttons.some((t) => t.includes("Done"))).toBe(true);
  });

  it("an id with no connection prefix shows only the name line", async () => {
    await render(<ModelChainInput field={field()} value={["protolabs/fast"]} onChange={vi.fn()} />);
    expect(q(".model-chain__id-prefix")).toBeNull();
    expect(text(q(".model-chain__id-name"))).toBe("protolabs/fast");
  });

  it("the inherited toggletip popover lists one full id per item on a phone", async () => {
    await render(
      <ModelChainInput
        field={field({ key: "compaction.model", label: "Compaction", role: "compaction", inherits: "aux" })}
        value={[]}
        onChange={vi.fn()}
        parentChain={["anthropic-oauth:claude-haiku-5-5", "protolabs/fast"]}
      />,
    );
    await click(q(".model-chain__inherits"));
    expect(qa(".model-chain__popover-list .model-chain__id").map((li) => li.textContent)).toEqual([
      "anthropic-oauth:claude-haiku-5-5",
      "protolabs/fast",
    ]);
  });
});

// ── criterion 9 (and the §2-item-11 / criterion-7 CSS rule) ─────────────────────────────────────
describe("ids never clip (criterion 9), and the CSS is tokens-only (criterion 7)", () => {
  // Every rule whose selector names the control, comments stripped (so a comment that merely
  // MENTIONS the banned declaration can't be mistaken for a real rule using it).
  const chainRules = settingsCss
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("}")
    .filter((block) => block.includes("model-chain"))
    .join("}");

  it("no model-chain rule uses text-overflow: ellipsis — ids wrap instead", () => {
    expect(/text-overflow\s*:\s*ellipsis/.test(chainRules)).toBe(false);
    expect(chainRules).toContain("overflow-wrap: anywhere");
  });

  it("the phone layout floors buttons and comboboxes at 44px", () => {
    expect(chainRules).toContain("min-height: 44px");
  });

  it("every id text's scrollWidth is at most its clientWidth at 390px", async () => {
    stubMatchMedia(true);
    await render(
      <ModelChainInput field={field()} value={["anthropic-oauth:claude-haiku-5-5"]} onChange={vi.fn()} />,
    );
    const ids = qa<HTMLElement>(".model-chain__id");
    expect(ids.length).toBeGreaterThan(0);
    for (const el of ids) expect(el.scrollWidth).toBeLessThanOrEqual(el.clientWidth);
  });
});

// ── pure helpers ─────────────────────────────────────────────────────────────────────────────
describe("helpers", () => {
  it("splitModelId separates the connection prefix on the first colon", () => {
    expect(splitModelId("anthropic-oauth:claude-haiku-5-5")).toEqual({
      prefix: "anthropic-oauth",
      name: "claude-haiku-5-5",
    });
    expect(splitModelId("protolabs/fast")).toEqual({ prefix: "", name: "protolabs/fast" });
  });

  it("foldLegacyFallbacks appends only the legacy models not already in the chain (idempotent)", () => {
    expect(foldLegacyFallbacks(["a"], ["b", "c"])).toEqual(["a", "b", "c"]);
    expect(foldLegacyFallbacks(["a", "b"], ["b"])).toEqual(["a", "b"]); // already present → no dupe
    expect(foldLegacyFallbacks(["a"], undefined)).toEqual(["a"]);
  });
});
