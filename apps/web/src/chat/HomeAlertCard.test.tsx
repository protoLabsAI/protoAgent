// The Home alert card (#5545): a brief, an alert or a decision pointer the server wrote into
// Home's thread renders as ONE card — icon, one-line title, "Urgent" for high importance, the
// time and a link to what raised it. Mounted with createRoot/act, like the other console suites.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage, HomeEntry } from "../lib/types";
import { useUI } from "../state/uiStore";

const openNotice = vi.hoisted(() => vi.fn(async () => true));
vi.mock("../app/NoticeClickWatch", () => ({ openNotice }));

import { HomeAlertCard, homeEntryText } from "./HomeAlertCard";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SENT = Date.UTC(2026, 10, 19, 14, 5);
const HEAD = (kind: string, source: string, importance: string) =>
  `[Home entry: ${kind} from ${source}, importance ${importance}. For reference; not a request.]`;

let container: HTMLElement;
let root: Root;

function render(home: HomeEntry, content: string, extra: Partial<ChatMessage> = {}) {
  const message: ChatMessage = { id: "home-entry-1", role: "user", content, createdAt: SENT, home, ...extra };
  act(() => root.render(h(HomeAlertCard, { message, home })));
}

const fleetDown: HomeEntry = { kind: "notify", importance: "high", source: "fleet-health", notice_id: "n-1" };
const FLEET_TEXT = `${HEAD("notify", "fleet-health", "high")}\nFleet member sol stopped\n\nOpen the console to check its logs.\n\nhttps://console.example/app/`;
const decision: HomeEntry = { kind: "question", importance: "high", source: "decision", notice_id: "decision-d42" };
const DECISION_TEXT = `${HEAD("question", "decision", "high")}\nShip the release tonight?`;

const article = () => container.querySelector("article")!;
const sourceLink = () => container.querySelector<HTMLAnchorElement>("a.pl-link");
const button = (label: string) => [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })); });

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  openNotice.mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("HomeAlertCard", () => {
  it("renders an article with an h3 title, an aria-hidden icon, the time in .pl-chip-meta and a source link", () => {
    render(fleetDown, FLEET_TEXT);
    const card = article();
    expect(card).not.toBeNull();
    const title = card.querySelector("h3")!;
    expect(title.textContent).toBe("Fleet member sol stopped");
    const icon = card.querySelector("svg")!;
    expect(icon.getAttribute("aria-hidden")).toBe("true");
    const time = card.querySelector("time.pl-chip-meta")!;
    expect(time.getAttribute("datetime")).toBe(new Date(SENT).toISOString());
    expect(time.textContent).toBe(new Date(SENT).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
    expect(card.querySelector(".pl-message__ts")).toBeNull();
    const link = sourceLink()!;
    expect(link.classList.contains("pl-link")).toBe(true);
    expect(link.textContent).toBe("Fleet member");
    expect(link.getAttribute("href")).toBe("https://console.example/app/");
  });

  it("shows the body without the model's reference head or the link line", () => {
    render(fleetDown, FLEET_TEXT);
    expect(container.querySelector(".pl-home-alert-body")!.textContent).toBe("Open the console to check its logs.");
    expect(container.textContent).not.toContain("[Home entry");
    expect(container.textContent).not.toContain("https://console.example");
  });

  it("is never the operator's bubble, though the entry is stored as a human message", () => {
    render(fleetDown, FLEET_TEXT);
    expect(container.querySelector(".pl-message--user")).toBeNull();
    expect(container.querySelector(".pl-message--system.pl-home-alert")).not.toBeNull();
  });

  it('shows "Urgent" only for high importance, with the warning tone on the icon', () => {
    render(fleetDown, FLEET_TEXT);
    const badge = container.querySelector(".pl-badge")!;
    expect(badge.textContent).toBe("Urgent");
    expect(badge.className).toBe("pl-badge"); // neutral: the icon carries the tone, not the badge
    expect(container.querySelector("h3")!.nextElementSibling).toBe(badge);
    expect(container.querySelector("svg")!.classList.contains("pl-home-alert-icon--urgent")).toBe(true);

    for (const importance of ["normal", "low"]) {
      render({ ...fleetDown, importance }, FLEET_TEXT);
      expect(container.querySelector(".pl-badge")).toBeNull();
      expect(container.textContent).not.toContain("Urgent");
      expect(container.querySelector("svg")!.classList.contains("pl-home-alert-icon--urgent")).toBe(false);
    }
  });

  it('gives a decision pointer an "Answer" action that opens the decision', () => {
    render(decision, DECISION_TEXT);
    expect(container.querySelector("h3")!.textContent).toBe("Ship the release tonight?");
    expect(sourceLink()!.getAttribute("href")).toBe("?notice=decision-d42");
    click(button("Answer")!);
    expect(openNotice).toHaveBeenCalledWith("decision-d42");
    click(sourceLink()!);
    expect(openNotice).toHaveBeenCalledTimes(2);
  });

  it('shows no "Answer" on an alert or a brief', () => {
    render(fleetDown, FLEET_TEXT);
    expect(button("Answer")).toBeUndefined();
    render({ kind: "review", importance: "normal", source: "scheduler", notice_id: "n-2" }, `${HEAD("review", "scheduler", "normal")}\nScheduled run digest completed.`);
    expect(button("Answer")).toBeUndefined();
    expect(sourceLink()!.textContent).toBe("Job run");
  });

  it("opens Settings ▸ Fleet from a fleet alert's source link, and leaves a modified click to the browser", () => {
    render(fleetDown, FLEET_TEXT);
    const open = vi.spyOn(useUI.getState(), "openGlobalSettings");
    try {
      const modified = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, metaKey: true });
      // jsdom does not navigate; a listener stops the default so the test only reads the card's choice.
      let cardPrevented = false;
      container.addEventListener("click", (e) => { cardPrevented = e.defaultPrevented; e.preventDefault(); }, { once: true });
      act(() => { sourceLink()!.dispatchEvent(modified); });
      expect(cardPrevented).toBe(false);
      expect(open).not.toHaveBeenCalled();
      click(sourceLink()!);
      expect(open).toHaveBeenCalledWith("fleet");
    } finally {
      open.mockRestore();
    }
  });

  it("drops a link that is not http(s), and the time when the entry has none", () => {
    render({ kind: "notify", importance: "normal", source: "spend-cap" }, "Soft limit passed\n\njavascript:alert(1)", { createdAt: undefined });
    expect(sourceLink()).toBeNull();
    expect(container.querySelector(".pl-chip-meta")).toBeNull();
    render({ kind: "notify", importance: "normal", source: "spend-cap" }, "Soft limit passed\n\n/app/");
    expect(sourceLink()!.textContent).toBe("Spend cap");
    expect(new URL(sourceLink()!.href).pathname).toBe("/app/");
  });
});

describe("homeEntryText", () => {
  it("splits the stored text into title, body and link", () => {
    expect(homeEntryText(FLEET_TEXT)).toEqual({
      title: "Fleet member sol stopped",
      body: "Open the console to check its logs.",
      link: "https://console.example/app/",
    });
    expect(homeEntryText(DECISION_TEXT)).toEqual({ title: "Ship the release tonight?", body: "", link: null });
    expect(homeEntryText("Title\nsecond line\n\nmore")).toEqual({ title: "Title", body: "second line\n\nmore", link: null });
    expect(homeEntryText("")).toEqual({ title: "", body: "", link: null });
  });
});
