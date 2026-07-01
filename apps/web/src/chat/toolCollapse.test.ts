import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  __resetToolCollapseWalk,
  resolveToggle,
  toggleLatestToolBlock,
  topLevelToggles,
} from "./toolCollapse";

describe("resolveToggle", () => {
  it("no-ops when there are no tool blocks", () => {
    expect(resolveToggle([], null)).toBeNull();
  });

  it("idle → targets the latest (last) block and expands it", () => {
    expect(resolveToggle([false, false, false], null)).toEqual({
      index: 2,
      expand: true,
      nextCursor: 2,
    });
  });

  it("an out-of-range cursor is treated as idle (→ latest)", () => {
    expect(resolveToggle([false, false], 9)).toEqual({ index: 1, expand: true, nextCursor: 1 });
    expect(resolveToggle([false, false], -5)).toEqual({ index: 1, expand: true, nextCursor: 1 });
  });

  it("expanded target → collapses it and walks the cursor upward", () => {
    // Latest is open → collapse it, cursor moves up to the previous block.
    expect(resolveToggle([false, false, true], 2)).toEqual({
      index: 2,
      expand: false,
      nextCursor: 1,
    });
  });

  it("after collapsing, the walked-to previous block is expanded", () => {
    expect(resolveToggle([false, false, false], 1)).toEqual({
      index: 1,
      expand: true,
      nextCursor: 1,
    });
  });

  it("collapsing the topmost block wraps the cursor out of range (next press → latest)", () => {
    const collapseTop = resolveToggle([true, false, false], 0);
    expect(collapseTop).toEqual({ index: 0, expand: false, nextCursor: -1 });
    // Next press: -1 is out of range → idle → latest again.
    expect(resolveToggle([false, false, false], collapseTop!.nextCursor)).toEqual({
      index: 2,
      expand: true,
      nextCursor: 2,
    });
  });

  it("drives the full toggle-latest → walk-upward sequence", () => {
    // Three collapsed blocks; simulate presses by threading the cursor + flipping state.
    const expanded = [false, false, false];
    let cursor: number | null = null;
    const press = () => {
      const plan = resolveToggle(expanded, cursor)!;
      expanded[plan.index] = plan.expand;
      cursor = plan.nextCursor;
      return plan;
    };
    expect(press()).toMatchObject({ index: 2, expand: true }); // expand latest
    expect(press()).toMatchObject({ index: 2, expand: false }); // collapse latest
    expect(press()).toMatchObject({ index: 1, expand: true }); // walk up → expand prev
    expect(press()).toMatchObject({ index: 1, expand: false }); // collapse prev
    expect(press()).toMatchObject({ index: 0, expand: true }); // walk up → expand oldest
    expect(expanded).toEqual([true, false, false]);
  });
});

// ── DOM integration (jsdom): the walk over the rendered chat DOM ────────────────────────────

// A minimal stand-in for a DS ToolCard head: a button whose aria-expanded flips on click,
// matching the DS `ToolCard` disclosure the real action clicks.
function toolCard(open = false): HTMLElement {
  const card = document.createElement("div");
  card.className = "pl-toolcard";
  const row = document.createElement("div");
  row.className = "pl-toolcard__head-row";
  const head = document.createElement("button");
  head.className = "pl-toolcard__head";
  head.setAttribute("aria-expanded", String(open));
  head.addEventListener("click", () => {
    head.setAttribute("aria-expanded", head.getAttribute("aria-expanded") === "true" ? "false" : "true");
  });
  row.appendChild(head);
  card.appendChild(row);
  return card;
}

// A reasoning card renders a DS ToolCard (same `.pl-toolcard__head`) inside a
// `.reasoning-card` wrapper — it must NOT count as a tool block (#1526).
function reasoningCard(open = false): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "reasoning-card";
  wrap.appendChild(toolCard(open));
  return wrap;
}

function assistantMessage(cards: HTMLElement[]): HTMLElement {
  const msg = document.createElement("div");
  msg.className = "pl-message pl-message--assistant";
  const list = document.createElement("div");
  list.className = "tool-calls pl-toolcard-list";
  cards.forEach((c) => list.appendChild(c));
  msg.appendChild(list);
  return msg;
}

function chatSurface(messages: HTMLElement[]): HTMLElement {
  const slot = document.createElement("div");
  slot.className = "chat-session-slot";
  messages.forEach((m) => slot.appendChild(m));
  document.body.appendChild(slot);
  return slot;
}

function headStates(slot: HTMLElement): boolean[] {
  return Array.from(slot.querySelectorAll(".pl-toolcard__head")).map(
    (h) => h.getAttribute("aria-expanded") === "true",
  );
}

describe("toggleLatestToolBlock (DOM)", () => {
  beforeEach(() => __resetToolCollapseWalk());
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("no-ops when there is no chat surface or no tool blocks", () => {
    expect(() => toggleLatestToolBlock()).not.toThrow();
    chatSurface([assistantMessage([])]); // a message with zero tool cards
    expect(() => toggleLatestToolBlock()).not.toThrow();
  });

  it("expands the latest block, collapses it, then walks upward", () => {
    const slot = chatSurface([assistantMessage([toolCard(), toolCard(), toolCard()])]);

    toggleLatestToolBlock();
    expect(headStates(slot)).toEqual([false, false, true]); // expand latest

    toggleLatestToolBlock();
    expect(headStates(slot)).toEqual([false, false, false]); // collapse latest

    toggleLatestToolBlock();
    expect(headStates(slot)).toEqual([false, true, false]); // walk up → expand previous

    toggleLatestToolBlock();
    expect(headStates(slot)).toEqual([false, false, false]); // collapse previous

    toggleLatestToolBlock();
    expect(headStates(slot)).toEqual([true, false, false]); // walk up → expand oldest
  });

  it("targets the LAST message that has tool blocks", () => {
    const older = assistantMessage([toolCard()]);
    const newer = assistantMessage([toolCard(), toolCard()]);
    const slot = chatSurface([older, newer]);

    toggleLatestToolBlock();
    // Only the newer message's latest card opened; the older one is untouched.
    expect(headStates(older)).toEqual([false]);
    expect(headStates(newer)).toEqual([false, true]);
  });

  it("ignores cards nested inside another block's body (subagent children / summary members)", () => {
    const parent = toolCard();
    const body = document.createElement("div");
    body.className = "pl-toolcard__body";
    body.appendChild(toolCard()); // a nested child card — not a top-level block
    parent.appendChild(body);
    const msg = assistantMessage([parent]);
    expect(topLevelToggles(msg)).toHaveLength(1); // only the parent counts

    const slot = chatSurface([msg]);
    toggleLatestToolBlock();
    // The parent (top-level) opened; the nested child stayed closed.
    const [parentOpen, childOpen] = headStates(slot);
    expect(parentOpen).toBe(true);
    expect(childOpen).toBe(false);
  });

  it("skips body-less (disabled) cards", () => {
    const disabled = toolCard();
    disabled.querySelector("button")!.setAttribute("disabled", "");
    disabled.querySelector("button")!.removeAttribute("aria-expanded");
    const real = toolCard();
    const msg = assistantMessage([disabled, real]);
    expect(topLevelToggles(msg)).toHaveLength(1);

    chatSurface([msg]);
    toggleLatestToolBlock();
    expect(real.querySelector("button")!.getAttribute("aria-expanded")).toBe("true");
  });

  it("excludes reasoning cards — a reasoning-only turn is a no-op", () => {
    const reasoning = reasoningCard();
    const msg = assistantMessage([reasoning]);
    expect(topLevelToggles(msg)).toHaveLength(0); // reasoning is not a tool block

    chatSurface([msg]);
    toggleLatestToolBlock();
    // The reasoning card stays closed — Cmd+O had nothing to toggle.
    expect(reasoning.querySelector("button")!.getAttribute("aria-expanded")).toBe("false");
  });

  it("walks tool blocks only, skipping a leading reasoning card", () => {
    const reasoning = reasoningCard();
    const tool = toolCard();
    const msg = assistantMessage([reasoning, tool]); // reasoning THEN a real tool call
    expect(topLevelToggles(msg)).toHaveLength(1); // only the tool call counts

    chatSurface([msg]);
    toggleLatestToolBlock();
    expect(tool.querySelector("button")!.getAttribute("aria-expanded")).toBe("true");
    expect(reasoning.querySelector("button")!.getAttribute("aria-expanded")).toBe("false");
  });
});
