// #4259 — a submitted HITL form answer renders as a persistent, labelled card (schema titles,
// option labels, Yes/No booleans, em-dash empties, grouped wizard steps) WITHOUT changing the
// byte-identical resume JSON the server receives. This pins the three moving parts as one
// vertical slice: the pure mapper (useHitl.describeFormResponse / displayFieldValue), the render
// in the shared ChatMessageView, and the persistence round-trip + payload invariance.
// Same minimal createRoot/act renderers the sibling console suites use (no testing-library dep).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatSession } from "./chat-store";
import { sanitizeFormResponse, sanitizePersisted } from "./chat-store";
import { ChatMessageView } from "./ChatMessageView";
import { describeFormResponse, displayFieldValue, useHitl, type HitlRunTurn, type UseHitlOptions } from "./useHitl";
import type { ChatMessage, HitlPayload } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A two-step wizard exercising every value shape the mapper must label.
const wizard: HitlPayload = {
  kind: "form",
  title: "Trip planner",
  steps: [
    {
      title: "Destination",
      schema: {
        properties: {
          city: { title: "City", type: "string" },
          mode: {
            title: "Travel mode",
            oneOf: [
              { const: "air", title: "By air" },
              { const: "rail", title: "By rail" },
            ],
          },
        },
        required: ["city"],
      },
    },
    {
      title: "Preferences",
      schema: {
        properties: {
          perks: {
            title: "Perks",
            type: "array",
            items: {
              oneOf: [
                { const: "wifi", title: "Wi-Fi" },
                { const: "meal", title: "Hot meal" },
              ],
            },
          },
          window: { title: "Window seat", type: "boolean" },
          notes: { title: "Notes", type: "string" },
        },
      },
    },
  ],
};

const wizardAnswers = { city: "Paris", mode: "rail", perks: ["wifi", "meal"], window: true, notes: "" };

describe("describeFormResponse — schema-derived labels (#4259)", () => {
  it("labels fields by schema title, resolves oneOf option titles (single + multi), Yes/No, em dash, and groups by step", () => {
    const display = describeFormResponse(wizard, wizardAnswers);
    expect(display).toEqual({
      title: "Trip planner",
      groups: [
        {
          title: "Destination",
          fields: [
            { label: "City", value: "Paris" },
            { label: "Travel mode", value: "By rail" },
          ],
        },
        {
          title: "Preferences",
          fields: [
            { label: "Perks", value: "Wi-Fi, Hot meal" },
            { label: "Window seat", value: "Yes" },
            { label: "Notes", value: "—" },
          ],
        },
      ],
    });
  });

  it("a single-step form yields ONE untitled group (step titles are a multi-step affordance)", () => {
    const single: HitlPayload = {
      kind: "form",
      title: "Confirm",
      steps: [{ title: "Only step", schema: { properties: { ok: { title: "Proceed?", type: "boolean" } } } }],
    };
    const display = describeFormResponse(single, { ok: false });
    expect(display).toEqual({ title: "Confirm", groups: [{ fields: [{ label: "Proceed?", value: "No" }] }] });
  });

  it("falls back to raw keys when a stepped form's fields (or whole step schema) carry no title", () => {
    // A real form (has steps) whose step schema is empty / untitled: field titles are missing,
    // so each answer is labelled by its raw property key rather than dropped.
    const partial: HitlPayload = {
      kind: "form",
      title: "Quick ask",
      steps: [{ schema: { properties: { reason: { type: "string" } } } }],
    };
    expect(describeFormResponse(partial, { reason: "ran long", count: 3 })).toEqual({
      title: "Quick ask",
      groups: [
        // The declared (untitled) field keeps schema order; the undeclared `count` is a
        // raw-key row appended in a trailing group.
        { fields: [{ label: "reason", value: "ran long" }] },
        { fields: [{ label: "count", value: "3" }] },
      ],
    });
  });

  it("is undefined for an ask_human shape (kind:form but no steps) given an object", () => {
    expect(describeFormResponse({ kind: "form", title: "Ask", question: "Why?" }, { any: "thing" })).toBeUndefined();
  });

  it("appends answer keys the schema never declared as raw-key rows, after the schema fields", () => {
    const display = describeFormResponse(wizard, { ...wizardAnswers, surprise: "extra" });
    const labels = display!.groups.flatMap((g) => g.fields.map((f) => f.label));
    expect(labels).toEqual(["City", "Travel mode", "Perks", "Window seat", "Notes", "surprise"]);
  });

  it("returns undefined for a string answer or an empty object (nothing to label → legacy text)", () => {
    expect(describeFormResponse(wizard, "yes")).toBeUndefined();
    expect(describeFormResponse(wizard, {})).toBeUndefined();
  });
});

describe("displayFieldValue — value shapes (#4259)", () => {
  it("empty multi-select and empty scalar render as an em dash; an unknown const shows verbatim", () => {
    const multi = { type: "array", items: { oneOf: [{ const: "a", title: "Alpha" }] } };
    expect(displayFieldValue(multi, [])).toBe("—");
    expect(displayFieldValue(multi, ["a", "zz"])).toBe("Alpha, zz");
    expect(displayFieldValue({ type: "boolean" }, false)).toBe("No");
    expect(displayFieldValue(undefined, "")).toBe("—");
    expect(displayFieldValue(undefined, 0)).toBe("0");
  });
});

// ── render: the card in the shared ChatMessageView ──────────────────────────────
let container: HTMLElement;
let root: Root;

function renderMessage(message: ChatMessage) {
  act(() => root.render(h(ChatMessageView, { message })));
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as never;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("ChatMessageView — form-response card (#4259)", () => {
  const formMessage = (): ChatMessage => ({
    id: "u-form",
    role: "user",
    content: JSON.stringify(wizardAnswers),
    status: "done",
    formResponse: describeFormResponse(wizard, wizardAnswers),
  });

  it("renders the labelled card in place of the raw resume JSON", () => {
    renderMessage(formMessage());
    const card = container.querySelector('[data-testid="hitl-response"]');
    expect(card).not.toBeNull();
    const text = card!.textContent ?? "";
    expect(text).toContain("Trip planner");
    expect(text).toContain("Destination");
    expect(text).toContain("City");
    expect(text).toContain("Travel mode");
    expect(text).toContain("By rail");
    expect(text).toContain("Wi-Fi, Hot meal");
    expect(text).toContain("Window seat");
    expect(text).toContain("Yes");
    // The raw JSON is NOT shown anywhere in the bubble.
    expect(container.textContent).not.toContain('"city"');
    expect(container.textContent).not.toContain('{"');
  });

  it("a user message WITHOUT the label map keeps its legacy text — arbitrary JSON is never sniffed into a card", () => {
    renderMessage({ id: "u-raw", role: "user", content: '{"city":"Paris"}', status: "done" });
    expect(container.querySelector('[data-testid="hitl-response"]')).toBeNull();
    expect(container.textContent).toContain('{"city":"Paris"}');
  });

  it("an empty / malformed label map falls back to legacy text (no broken card)", () => {
    renderMessage({ id: "u-empty", role: "user", content: "hi", status: "done", formResponse: { groups: [] } });
    expect(container.querySelector('[data-testid="hitl-response"]')).toBeNull();
    expect(container.textContent).toContain("hi");
  });
});

// ── persistence round-trip + defensive sanitize ─────────────────────────────────
describe("form-response persistence (#4259)", () => {
  const session = (over: Partial<ChatSession> = {}): ChatSession => ({
    id: "s1",
    title: "Chat",
    createdAt: 1,
    updatedAt: 2,
    messages: [],
    ...over,
  });

  it("survives a sanitizePersisted round-trip unchanged (reload / session switch)", () => {
    const display = describeFormResponse(wizard, wizardAnswers)!;
    const msg: ChatMessage = { id: "u-form", role: "user", content: JSON.stringify(wizardAnswers), status: "done", formResponse: display };
    const blob = { version: 1, sessions: [session({ messages: [msg] })], currentSessionId: "s1" };
    const restored = sanitizePersisted(JSON.parse(JSON.stringify(blob)));
    const out = restored!.sessions[0].messages[0];
    // Byte-identical content (the server resume value) AND the label map both survive.
    expect(out.content).toBe(JSON.stringify(wizardAnswers));
    expect(out.formResponse).toEqual(display);
  });

  it("sanitizeFormResponse keeps a well-formed map and rejects malformed ones", () => {
    const good = { title: "T", groups: [{ title: "G", fields: [{ label: "L", value: "V" }] }] };
    expect(sanitizeFormResponse(good)).toEqual(good);
    expect(sanitizeFormResponse({ groups: [{ fields: [{ label: "L", value: "V" }] }] })).toEqual({
      groups: [{ fields: [{ label: "L", value: "V" }] }],
    });
    // A group with non-string field values, or no groups, is not renderable → dropped.
    expect(sanitizeFormResponse({ groups: [{ fields: [{ label: "L", value: 1 }] }] })).toBeUndefined();
    expect(sanitizeFormResponse({ groups: [] })).toBeUndefined();
    expect(sanitizeFormResponse("nope")).toBeUndefined();
  });

  it("a corrupt label map is stripped on load but the message (and its content) is kept", () => {
    const msg = { id: "u-bad", role: "user", content: '{"x":1}', status: "done", formResponse: { groups: "oops" } };
    const blob = { version: 1, sessions: [session({ messages: [msg as unknown as ChatMessage] })], currentSessionId: "s1" };
    const out = sanitizePersisted(JSON.parse(JSON.stringify(blob)))!.sessions[0].messages[0];
    expect(out.content).toBe('{"x":1}');
    expect(out.formResponse).toBeUndefined();
  });
});

// ── resume payload invariance (the server contract is untouched) ─────────────────
let hookContainer: HTMLElement;
let hookRoot: Root;
let result: { current: ReturnType<typeof useHitl> };

function renderHook(opts: UseHitlOptions) {
  result = { current: undefined as unknown as ReturnType<typeof useHitl> };
  const Probe = (props: UseHitlOptions) => {
    result.current = useHitl(props);
    return null;
  };
  act(() => hookRoot.render(h(Probe, opts)));
  return result;
}

describe("resumeHitl — byte-identical resume value, card metadata alongside (#4259)", () => {
  const mkRun = () => vi.fn<HitlRunTurn>(async () => {});
  const baseOpts = (over: Partial<UseHitlOptions> = {}): UseHitlOptions => ({
    sessionId: "sess-1",
    runTurn: mkRun(),
    noteToThread: vi.fn(),
    lastAssistantId: () => "asst-1",
    ...over,
  });

  beforeEach(() => {
    hookContainer = document.createElement("div");
    document.body.appendChild(hookContainer);
    hookRoot = createRoot(hookContainer);
  });
  afterEach(() => {
    act(() => hookRoot.unmount());
    hookContainer.remove();
  });

  it("sends exactly JSON.stringify(response) and carries the label map as a presentation-only opt", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl(wizard));
    await act(() => r.current.resumeHitl(wizardAnswers));
    const [content, opts] = runTurn.mock.calls[0];
    expect(content).toBe(JSON.stringify(wizardAnswers)); // byte-identical wire value
    expect(opts).toEqual({ hitlResume: true, formResponse: describeFormResponse(wizard, wizardAnswers) });
  });

  it("an ask_human free-text answer stays a plain string with NO card metadata", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl({ kind: "form", title: "Ask", question: "Why?" }));
    await act(() => r.current.resumeHitl("because"));
    expect(runTurn).toHaveBeenCalledWith("because", { hitlResume: true });
  });

  it("an approval resume is still silent, continues the paused bubble, and carries no card", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl({ kind: "approval", title: "Run it?", detail: "rm -rf build" }));
    await act(() => r.current.resumeHitl("approved"));
    expect(runTurn).toHaveBeenCalledWith("approved", { hidden: true, resumeMessageId: "asst-1", hitlResume: true });
  });
});
