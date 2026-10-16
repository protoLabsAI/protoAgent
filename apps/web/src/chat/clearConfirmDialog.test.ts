// #2996 — the "Clear this conversation?" confirm behind ⌘K (chat.clear) and /clear. Both
// entry points park a clear request in the store; ChatSurface folds it into this dialog, so
// the server clear + local updateMessages sequence starts ONLY on confirm. These pin the
// dialog's own contract: the exact copy, the single 3-way memory choice (the harvest/forget
// switches of #4053/#3493 became one RadioGroup — Save a summary / Don't save anything / Erase
// from memory), that cancel/backdrop dismiss without confirming, that confirm reports the
// mapped ChatMemoryChoice, that the options are mutually exclusive by construction, and that an
// incognito chat drops the "Save a summary" option for a "never saved" note (#4053).
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so elements are built with React.createElement, not JSX). Same pattern as
// app/AuthGate.test.ts, which likewise drives a portaled DS Dialog.
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClearConversationDialog } from "./ClearConversationDialog";

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
});

function mount(node: Parameters<Root["render"]>[0]) {
  act(() => {
    root.render(node);
  });
}

// @protolabsai/ui portals the Dialog to <body>, so its content is a SIBLING of `container`.
function buttonByText(text: string) {
  return [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);
}

function radio(value: "summary" | "none" | "erase") {
  return document.body.querySelector<HTMLInputElement>(`.chat-memory-choice .pl-radio__input[value="${value}"]`);
}

describe("ClearConversationDialog (#2996, #4053, #3493)", () => {
  it("renders nothing while closed", () => {
    mount(h(ClearConversationDialog, { open: false, onConfirm: () => {}, onCancel: () => {} }));
    expect(document.body.querySelector(".pl-dialog")).toBeNull();
  });

  it("open on a regular chat: shows the exact copy, the 3 options, and selects 'Save a summary' by default (#4053)", () => {
    mount(h(ClearConversationDialog, { open: true, onConfirm: () => {}, onCancel: () => {} }));
    const dialog = document.body.querySelector(".pl-dialog");
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("This conversation will be cleared. This can't be undone.");
    // The three plain-language options, each with its one-line explanation.
    const text = dialog!.textContent!;
    expect(text).toContain("Save a summary");
    expect(text).toContain("Adds a searchable summary to the knowledge base.");
    expect(text).toContain("Don't save anything");
    expect(text).toContain("Anything already archived from this chat stays.");
    expect(text).toContain("Erase from memory");
    expect(text).toContain("Also removes summaries and archives this chat already saved.");
    // Save a summary is on by default (incognito is the keep-out-of-memory switch).
    expect(radio("summary")!.checked).toBe(true);
    expect(radio("none")!.checked).toBe(false);
    expect(radio("erase")!.checked).toBe(false);
    // No switches and no compaction jargon survive the redesign.
    expect(document.body.querySelector(".pl-switch__input")).toBeNull();
    expect(text).not.toContain("/compact");
    expect(text).not.toContain("Harvest");
  });

  it("Cancel dismisses WITHOUT confirming (dismissing SHALL NOT delete)", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel }));
    act(() => buttonByText("Cancel")!.click());
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Confirm reports harvest=true when 'Save a summary' is left selected (default, #4053)", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith({ harvest: true, forget: false });
  });

  it("'Don't save anything' confirms harvest=false with no forget", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => radio("none")!.click());
    expect(radio("none")!.checked).toBe(true);
    expect(radio("summary")!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: false });
  });

  // #3493: "Erase from memory" removes the archives/summaries this chat already saved, and its
  // copy says so — the honesty the old compaction paragraph used to carry.
  it("'Erase from memory' confirms forget=true, harvest=false", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => radio("erase")!.click());
    expect(radio("erase")!.checked).toBe(true);
    expect(radio("summary")!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: true });
  });

  it("the radio is mutually exclusive by construction: picking erase then summary ends on harvest=true", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => radio("erase")!.click());
    act(() => radio("summary")!.click());
    expect(radio("summary")!.checked).toBe(true);
    expect(radio("erase")!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: true, forget: false });
  });

  // #4053 incognito: never saved — no "Save a summary" option, a note in its place, and "Don't
  // save anything" preselected; confirm sends harvest=false.
  it("incognito chat: hides 'Save a summary', shows the note, preselects 'Don't save anything', confirms harvest=false", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, incognito: true, onConfirm, onCancel: () => {} }));
    const text = document.body.querySelector(".pl-dialog")!.textContent!;
    expect(radio("summary")).toBeNull();
    expect(text).toContain("Incognito — never saved to memory.");
    expect(radio("none")!.checked).toBe(true);
    expect(radio("erase")!.checked).toBe(false);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: false });
  });

  it("incognito chat: 'Erase from memory' still reaches the server as forget=true, harvest stays false", () => {
    const onConfirm = vi.fn();
    mount(h(ClearConversationDialog, { open: true, incognito: true, onConfirm, onCancel: () => {} }));
    act(() => radio("erase")!.click());
    expect(radio("erase")!.checked).toBe(true);
    act(() => buttonByText("Clear conversation")!.click());
    expect(onConfirm).toHaveBeenCalledWith({ harvest: false, forget: true });
  });

  it("re-initialises the choice from the chat's incognito flag each time it reopens (#4053)", () => {
    const onConfirm = vi.fn();
    // Open on a regular chat and move off the default.
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    act(() => radio("erase")!.click());
    expect(radio("erase")!.checked).toBe(true);
    expect(radio("summary")!.checked).toBe(false);
    // Close, then reopen on the same regular chat — the default re-arms (Save a summary).
    mount(h(ClearConversationDialog, { open: false, onConfirm, onCancel: () => {} }));
    mount(h(ClearConversationDialog, { open: true, onConfirm, onCancel: () => {} }));
    expect(radio("summary")!.checked).toBe(true);
    expect(radio("erase")!.checked).toBe(false);
    // Reopen as incognito — "Save a summary" gone, "Don't save anything" re-armed (no carryover).
    mount(h(ClearConversationDialog, { open: false, onConfirm, onCancel: () => {} }));
    mount(h(ClearConversationDialog, { open: true, incognito: true, onConfirm, onCancel: () => {} }));
    expect(radio("summary")).toBeNull();
    expect(radio("none")!.checked).toBe(true);
  });
});
