import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SettingsCategory } from "./SettingsCategory";
import { settingsSchemaQuery, type ModelRoleStatus } from "../lib/queries";
import { api } from "../lib/api";
import type { SettingsField } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const now = Date.parse("2026-11-16T12:00:00Z");
const event = { time: new Date(now - 7200000).toISOString(), from: ["connection:failed-model"],
  to: "connection:winning-model", reason: "connection:failed-model: RateLimitError (429)" };
const status = (role: string, overrides: Partial<ModelRoleStatus> = {}): ModelRoleStatus => ({
  role, chain: ["one", "two"], inherited: false, source: role, status: "ok", last_fallback: null, ...overrides,
});
const definitions = [
  ["main", "model.name", "Main", "Main and aux", ""],
  ["aux", "routing.aux_model", "Auxiliary", "Main and aux", "main"],
  ["compaction", "compaction.model", "Compaction", "Memory", "aux"],
  ["harvest", "memory.harvest_model", "Harvest", "Memory", "aux"],
  ["facts", "memory.facts_model", "Facts", "Memory", "harvest"],
  ["memory_select", "memory.select_model", "Memory select", "Memory", "aux"],
  ["memory_gist", "memory.gist_model", "Chat gist", "Memory", "aux"],
  ["goal_eval", "goal.eval_model", "Goal eval", "Checks", "aux"],
  ["soul_drift_judge", "soul.drift.judge.model", "Drift judge", "Checks", "aux"],
  ["image_describe", "knowledge.image_describe_model", "Image describe", "Media", "main"],
];
let host: HTMLDivElement, root: Root, client: QueryClient;
let roles: ModelRoleStatus[], names: string[], overrides: Record<string, unknown>, fail: boolean;
const buttons = () => [...document.querySelectorAll<HTMLButtonElement>("button")];
const button = (text: string) => buttons().find((b) => b.textContent?.trim() === text)!;
const row = (key: string) => host.querySelector(`[data-key="${key}"]`)!;
async function click(el: Element) { await act(async () => { (el as HTMLElement).click(); }); }
async function mount() {
  const fields: SettingsField[] = definitions.map(([role, key, label, subhead, inherits]) => ({
    role, key, label, subhead, inherits, type: "model_chain", section: "Model roles", scope: "agent", source: "agent",
    options: ["one", "two"], restart: false, value: role === "main" || role === "aux" ? ["one", "two"] : [],
  }));
  client.setQueryData(settingsSchemaQuery().queryKey, { groups: [
    { section: "Favorites", category: "Model", fields: [] },
    { section: "Model roles", category: "Model", fields: [...fields, { ...fields[0], role: undefined, subhead: undefined, key: "subagents", type: "dict", value: overrides }] },
  ] });
  vi.spyOn(api, "settingsSchema").mockResolvedValue(client.getQueryData(settingsSchemaQuery().queryKey)!);
  await act(async () => {
    root.render(<QueryClientProvider client={client}><ToastProvider>
      <SettingsCategory category="Model" lead={<p>Connected</p>} leadTitle="Connections" />
    </ToastProvider></QueryClientProvider>);
  });
  // Flush query notification batching without a real sleep.
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
}
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(now);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = () => {};
  roles = []; names = []; overrides = {}; fail = false;
  vi.spyOn(api, "subagents").mockImplementation(async () => ({ subagents: names.map((name, i) => ({
    name, enabled: i !== 1, description: "", tools: [], default_tools: [], max_turns: 2, default_max_turns: 2,
  })) }));
  vi.stubGlobal("fetch", vi.fn(async () => {
    if (fail) throw new Error("offline");
    return new Response(JSON.stringify({ roles }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount()); client.clear(); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers();
});

it.each([
  [[], {}, null],
  [["alpha"], {}, "1 subagent (alpha) · follows the chat's model, else aux"],
  [["alpha"], { alpha: { model: "two" } }, "1 subagent (alpha) · has its own chain"],
  [["alpha", "beta", "gamma"], { beta: { model: ["two"] } }, "3 subagents · 1 has its own chain · 2 follow the chat's model, else aux"],
  [["alpha", "beta"], {}, "2 subagents · 2 follow the chat's model, else aux"],
] as const)("uses registry order and counts: %j", async (registry, pins, summary) => {
  names = [...registry]; overrides = pins; await mount();
  const headings = [...host.querySelectorAll(".model-roles h3")].map((h) => h.textContent);
  expect(headings).toEqual(["Main and aux", "Memory", "Checks", "Media", ...(names.length ? ["Subagents"] : [])]);
  const text = host.textContent!;
  expect(text.indexOf("Connections")).toBeLessThan(text.indexOf("Model roles"));
  expect(text.indexOf("Model roles")).toBeLessThan(text.indexOf("Favorites"));
  if (!summary) { expect(host.querySelector(".model-roles__registry")).toBeNull(); return; }
  const trigger = button(summary); expect(trigger.getAttribute("aria-expanded")).toBe("false");
  await click(trigger); expect(trigger.getAttribute("aria-expanded")).toBe("true");
  const list = document.getElementById(trigger.getAttribute("aria-controls")!)!;
  expect([...list.querySelectorAll(".setting-label")].map((el) => el.textContent)).toEqual(names.map((name, i) => name + (i === 1 ? " (off)" : "")));
  // Exact registry-only rows catch accidental built-in names without pinning source text.
  expect(list.querySelectorAll('[role="group"]').length).toBe(names.length);
});

it.each([false, true])("image status only appears with no-vision signal (%s)", async (unavailable) => {
  roles = [status("image_describe", unavailable ? { status: "unavailable", reason: "no-vision" } : {})];
  await mount();
  const image = row("knowledge.image_describe_model");
  expect(image.textContent).toContain("Inherits main");
  expect(image.textContent?.includes("unavailable")).toBe(unavailable);
  if (unavailable) { await click(button("unavailable")); expect(document.body.textContent).toContain("None of these models can read images."); }
});

it.each([true, false])("deduplicates only matching inherited events (%s)", async (same) => {
  roles = [status("aux", { last_fallback: event }), status("compaction", {
    source: "aux", inherited: true, last_fallback: same ? event : { ...event, reason: "connection:failed-model: TimeoutError" },
  })];
  await mount();
  expect(row("routing.aux_model").textContent).toContain("fell back 2h ago");
  expect(row("compaction.model").textContent?.includes("fell back 2h ago")).toBe(!same);
  expect(document.body.textContent).not.toContain(event.to);
  expect(document.body.textContent).not.toContain(event.reason);
  const trigger = same ? buttons().find((b) => b.getAttribute("aria-label")?.includes("the same as aux"))! : button("fell back 2h ago");
  await click(trigger);
  expect(document.body.textContent).toContain(event.to);
  expect(document.body.textContent).toContain(`${event.from[0]} was rate-limited`);
  if (same) expect(document.body.textContent).toContain("Same fallback as aux.");
});

it("expires fallback marks while open and ignores stale events", async () => {
  roles = [status("aux", { last_fallback: { ...event, time: new Date(now - 86400000 + 10000).toISOString() } }),
    status("compaction", { last_fallback: { ...event, time: new Date(now - 86400000).toISOString() } })];
  await mount(); expect(row("routing.aux_model").textContent).toContain("fell back");
  expect(row("compaction.model").textContent).not.toContain("fell back");
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(host.textContent).not.toContain("fell back");
});

it("keeps chains editable when history fails; facts explains transitive inheritance", async () => {
  fail = true; await mount();
  expect(host.textContent?.match(/Fallback history isn't available/g)).toHaveLength(1);
  expect(row("memory.facts_model").textContent).toContain("Inherits harvest");
  await click(button("Inherits harvest"));
  expect(document.body.textContent).toContain("which inherits aux's chain");
  await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
  for (const key of definitions.slice(2).map((d) => d[1])) {
    const own = [...row(key).querySelectorAll("button")].find((b) => b.textContent === "Set its own chain")!;
    await click(own); expect(row(key).querySelectorAll("input").length).toBe(2);
  }
});

it("saves subagent pins through the dictionary without losing sibling settings, and discards edits", async () => {
  names = ["alpha", "beta"]; overrides = { alpha: { tools: ["example"], max_turns: 9 }, beta: { enabled: false } };
  const save = vi.spyOn(api, "saveSettings").mockResolvedValue({ ok: true, messages: [], restart_required: [] });
  await mount(); await click(button("2 subagents · 2 follow the chat's model, else aux"));
  const group = host.querySelector('[aria-label="alpha model chain"]')!;
  await click([...group.querySelectorAll("button")].find((b) => b.textContent === "Set its own chain")!);
  expect(host.textContent).toContain("1 has its own chain");
  await click(button("Discard")); expect(host.textContent).toContain("2 follow the chat's model, else aux");
  await click([...group.querySelectorAll("button")].find((b) => b.textContent === "Set its own chain")!);
  await click(button("Save & apply"));
  expect(save).toHaveBeenCalledWith({ subagents: { ...overrides, alpha: { ...(overrides.alpha as object), model: ["one", "two"] } } }, "agent");
});
