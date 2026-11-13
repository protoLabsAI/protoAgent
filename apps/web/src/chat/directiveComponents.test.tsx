import { act, cloneElement, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ToastProvider } from "@protolabsai/ui/overlays";
import type { DirectiveRow } from "../lib/api/chat";
import type { DirectivesSnapshot, RevokeResult } from "./useDirectives";
import { useUI } from "../state/uiStore";
import { DirectiveStrip } from "./DirectiveStrip";
import { DirectiveDrawer } from "./DirectiveDrawer";
import { DirectiveEntry } from "./DirectiveEntry";
import css from "./chat.css?raw";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mocks = vi.hoisted(() => ({ mobile: false, refresh: vi.fn(), revoke: vi.fn() }));
let snapshot: DirectivesSnapshot;
vi.mock("./useDirectives", () => ({ useDirectives: () => ({ ...snapshot, refresh: mocks.refresh, revoke: mocks.revoke }) }));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => mocks.mobile }));
let host: HTMLDivElement;
let root: Root;
let view: ReactElement;
const close = vi.fn();
const row = (id: string, overrides: Partial<DirectiveRow> = {}): DirectiveRow => ({
  id, session_id: "s", quote: `Quote ${id}`, conditions: "", scope: "", status: "in_force",
  created_at: "2026-11-07T17:14:02+00:00", closed_at: null, closed_by: null, close_reason: null, ...overrides,
});
async function render(node = view) {
  view = node;
  await act(async () => root.render(<ToastProvider>{cloneElement(node)}</ToastProvider>));
}
const buttons = (text: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].filter((b) => b.textContent?.trim() === text);
async function click(text: string, index = 0) { await act(async () => buttons(text)[index].click()); }
const drawer = () => <DirectiveDrawer sessionId="s" open onClose={close} />;
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.mobile = false;
  snapshot = { state: "ready", active: [], history: [], loadedAt: Date.now() };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); document.body.replaceChildren(); vi.unstubAllGlobals(); });

it("keeps the same idle live region for loading, empty and history only", async () => {
  snapshot.state = "loading";
  await render(<DirectiveStrip sessionId="s" />);
  const node = host.querySelector('[role="status"]');
  for (const state of ["loading", "ready"] as const) {
    snapshot.state = state; await render();
    expect(node?.className).toContain("chat-ledger--idle");
  }
  snapshot.history = [row("history", { status: "fulfilled" })]; await render();
  expect(host.querySelector('[role="status"]')).toBe(node);
  expect(node?.textContent).toBe("");
});
it("renders singular, plural and mobile copy, full labels and opens the session", async () => {
  snapshot.active = [row("one")]; await render(<DirectiveStrip sessionId="s" />);
  expect(host.textContent).toContain('1 active instruction · "Quote one"');
  snapshot.active.push(row("two")); await render();
  expect(host.textContent).toContain('2 active instructions · "Quote one" and 1 more');
  expect(host.querySelector('[role="status"]')?.getAttribute("aria-label")).toBe("2 active instructions: Quote one; Quote two");
  await click("View"); expect(useUI.getState().surfaceStates.work.section).toBe("directives");
  mocks.mobile = true; await render(); expect(host.textContent).toContain('2 active · "Quote one"');
  expect(host.textContent).not.toContain("more");
  snapshot.state = "stale"; await render(); expect(buttons("View")).toHaveLength(1);
});
it("shows a nonidle failure row with Retry only before any successful load", async () => {
  snapshot.state = "failed"; await render(<DirectiveStrip sessionId="s" />);
  expect(host.textContent).toContain("Couldn't check standing instructions");
  expect(host.querySelector(".chat-ledger--idle")).toBeNull();
  await click("Retry"); expect(mocks.refresh).toHaveBeenCalled();
});
it("renders drawer loading, empty, failed, stale and history-only states and both sides", async () => {
  snapshot.state = "loading"; await render(drawer());
  expect(document.querySelectorAll(".pl-skel-lines").length).toBe(2);
  expect(document.querySelector(".pl-drawer--right")).not.toBeNull();
  snapshot.state = "ready"; await render(); expect(document.body.textContent).toContain("No standing instructions");
  snapshot.state = "failed"; await render();
  expect(document.body.textContent).toContain("Couldn't load standing instructions");
  expect(document.body.textContent).not.toContain("No standing instructions");
  expect(document.querySelector("h3")).toBeNull();
  await click("Retry"); expect(mocks.refresh).toHaveBeenCalled();
  snapshot.state = "stale"; snapshot.history = [row("old", { status: "fulfilled" })];
  mocks.mobile = true; await render();
  expect(document.querySelector(".pl-drawer--bottom")).not.toBeNull();
  expect(document.body.textContent).toContain("Showing what was loaded at");
  expect(document.body.textContent).toContain("Nothing is active right now.");
});
it("sorts history newest first, keeps it collapsed, and hides the default note and ids", async () => {
  snapshot.history = [row("secret-old", { quote: "Older", status: "fulfilled", closed_by: "agent", closed_at: "2026-11-06T00:00:00+00:00" }), row("secret-new", { quote: "Newer", status: "revoked", closed_at: "2026-11-07T00:00:00+00:00", closed_by: "operator", close_reason: "revoked by the operator" })];
  await render(drawer());
  expect(buttons("History (2)")[0].getAttribute("aria-expanded")).toBe("false");
  await click("History (2)");
  expect([...document.querySelectorAll("blockquote")].map((n) => n.textContent)).toEqual(["Newer", "Older"]);
  expect(buttons("Withdraw…")).toHaveLength(0);
  expect(document.querySelectorAll("h3")).toHaveLength(1);
  expect(document.body.textContent).not.toContain("Note:");
  expect(document.body.textContent).not.toContain("secret-");
  for (const badge of document.querySelectorAll(".pl-badge")) {
    expect(badge.hasAttribute("tone")).toBe(false);
    expect(badge.className).not.toMatch(/success|error/);
  }
});
it("prevents Escape from closing the drawer and returns focus to Withdraw", async () => {
  snapshot.active = [row("a")]; await render(drawer()); await click("Withdraw…");
  const group = document.querySelector('[role="group"]')!;
  expect(document.activeElement?.textContent).toBe("Withdraw this instruction?");
  const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  await act(async () => { group.dispatchEvent(escape); });
  expect(escape.defaultPrevented).toBe(true); expect(close).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(buttons("Withdraw…")[0]);
});
it.each([false, true])("focuses the next quote or heading after success (last=%s)", async (last) => {
  snapshot.active = last ? [row("a")] : [row("a"), row("b")];
  mocks.revoke.mockImplementation(async () => { snapshot.active = snapshot.active.slice(1); snapshot.history = [row("a", { status: "revoked" })]; await render(); return { ok: true }; });
  await render(drawer()); await click("Withdraw…"); await click("Withdraw");
  expect(document.activeElement?.tagName).toBe(last ? "H3" : "BLOCKQUOTE");
  if (!last) expect(document.activeElement?.textContent).toBe("Quote b");
  expect(document.body.textContent).toContain("Withdrawn. The agent won't treat it as approved from its next turn.");
});
it.each([409, 404])("preserves and focuses the %s callout across the hook refresh", async (status) => {
  snapshot.active = [row("a")];
  mocks.revoke.mockImplementation(async () => {
    snapshot.active = [];
    snapshot.history = status === 409 ? [row("a", { status: "fulfilled", closed_by: "agent", closed_at: "2026-11-08T17:14:00+00:00" })] : [];
    await render();
    return status === 409 ? { conflict: snapshot.history[0] } : { gone: true };
  });
  await render(drawer()); await click("Withdraw…"); await click("Withdraw");
  expect(document.activeElement?.getAttribute("tabindex")).toBe("-1");
  expect(document.activeElement?.textContent).toContain(status === 409 ? "This was already closed: Done · the agent marked it complete" : "This instruction is no longer in the list.");
});
it("keeps the reason on failure and disables cancellation while pending", async () => {
  let resolve!: (value: RevokeResult) => void;
  mocks.revoke.mockImplementation(() => new Promise<RevokeResult>((done) => { resolve = done; }));
  snapshot.active = [row("a")]; await render(drawer()); await click("Withdraw…");
  const field = document.querySelector("textarea")!;
  expect(field.maxLength).toBe(2000);
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(field, "Wait for Monday");
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click("Withdraw"); expect(buttons("Keep it")[0].disabled).toBe(true);
  expect(buttons("Withdraw")[0].disabled).toBe(true);
  const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  await act(async () => { field.dispatchEvent(escape); });
  expect(escape.defaultPrevented).toBe(true);
  expect(close).not.toHaveBeenCalled();
  expect(document.querySelector('[role="group"]')).not.toBeNull();
  await act(async () => resolve({ error: new Error("offline") }));
  expect(document.querySelector("textarea")?.value).toBe("Wait for Monday");
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("Couldn't withdraw it. It's still active.");
  expect(mocks.revoke).toHaveBeenCalledWith("a", "Wait for Monday");
  mocks.revoke.mockResolvedValue({ error: new Error("still offline") });
  await click("Try again");
  expect(mocks.revoke).toHaveBeenCalledTimes(2);
  expect(mocks.revoke).toHaveBeenLastCalledWith("a", "Wait for Monday");
  expect(document.querySelector("textarea")?.value).toBe("Wait for Monday");
});
it.each([false, true])("restores the opener or composer fallback on close (removed=%s)", async (removed) => {
  const opener = document.createElement("button"); document.body.append(opener); opener.focus();
  const hiddenSlot = document.createElement("div"); hiddenSlot.className = "chat-session-slot"; hiddenSlot.hidden = true;
  hiddenSlot.innerHTML = '<div class="composer-wrap"><textarea></textarea></div>'; document.body.append(hiddenSlot);
  const composer = document.createElement("div"); composer.className = "chat-session-slot";
  composer.innerHTML = '<div class="composer-wrap"><textarea></textarea></div>'; document.body.append(composer);
  await render(drawer()); if (removed) opener.remove();
  await render(<DirectiveDrawer sessionId="s" open={false} onClose={close} />);
  expect(document.activeElement).toBe(removed ? composer.querySelector("textarea") : opener);
});
it("preserves quote text, optional metadata, and uses CSS-only clamping", async () => {
  const quote = "A long instruction\n".repeat(12);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(300);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(100);
  await render(<DirectiveEntry entry={row("hidden-id", { quote, conditions: "approved", scope: "release" })} revoke={mocks.revoke} />);
  expect(document.querySelector("blockquote")?.textContent).toBe(quote);
  expect(document.body.textContent).toContain("Only if: approved");
  expect(document.body.textContent).toContain("Applies to: release");
  expect(buttons("Show full text")[0].getAttribute("aria-expanded")).toBe("false");
  await click("Show full text"); expect(buttons("Show less")[0].getAttribute("aria-expanded")).toBe("true");
  expect(css).toContain("-webkit-line-clamp: 8"); expect(css).toContain("white-space: pre-wrap");
  vi.restoreAllMocks();
});
it("orders active entries oldest first and divides them", async () => {
  snapshot.active = [row("new", { created_at: "2026-11-08T17:14:00+00:00" }), row("old")];
  await render(drawer());
  expect([...document.querySelectorAll("blockquote")].map((node) => node.textContent)).toEqual(["Quote old", "Quote new"]);
  expect(document.querySelectorAll(".pl-divider")).toHaveLength(1);
  expect(document.querySelector("h3")?.textContent).toBe("Active (2)");
});
it("shows explicit closure notes and omits empty conditions and scope", async () => {
  await render(<DirectiveEntry entry={row("secret-id", { quote: "Ship the release", status: "fulfilled", closed_by: "operator", close_reason: "Released", closed_at: "2026-11-07T19:02:00+00:00" })} revoke={mocks.revoke} />);
  expect(host.textContent).toContain("Done · you marked it complete");
  expect(host.textContent).toContain("Note: Released");
  expect(host.textContent).not.toContain("Only if:");
  expect(host.textContent).not.toContain("Applies to:");
  expect(host.textContent).not.toContain("secret-id");
});
it("an auto-fulfilled directive says it closed itself, with the attributed reason (ADR 0129 D4/Q2)", async () => {
  await render(<DirectiveEntry entry={row("auto", { quote: "Work #879 then #873", status: "fulfilled", closed_by: "system", close_reason: "acme/widgets#879 closed; acme/widgets#873 closed", closed_at: "2026-11-07T19:02:00+00:00" })} revoke={mocks.revoke} />);
  expect(host.textContent).toContain("Done · it closed itself when its linked work finished");
  expect(host.textContent).toContain("Note: acme/widgets#879 closed; acme/widgets#873 closed");
});
