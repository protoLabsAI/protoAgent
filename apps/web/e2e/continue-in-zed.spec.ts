import { expect, test, type Page } from "@playwright/test";

import { withCodePane } from "./codePane";

// "Continue in Zed" (chat tab menu): POSTs a hand-off for the current chat — scoped to the
// code pane's project + file when one is open — and toasts the operator to start a thread in
// Zed within 2 minutes (the protoagent-acp shim claims it). Only offered while the external
// editor preference is Zed. Mock routes: e2e/mock-server.mjs (`/api/editor/handoff`).

async function send(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

async function openTabMenu(page: Page) {
  await page.locator(".pl-tabbar__tab").first().click({ button: "right" });
  const menu = page.locator(".pl-menu");
  await expect(menu).toBeVisible();
  return menu;
}

function handoffRequest(page: Page) {
  return page.waitForRequest((r) => r.url().endsWith("/api/editor/handoff") && r.method() === "POST");
}

test("with no file open: offers the chat for any folder and says to switch to Zed", async ({ page }) => {
  await send(page, "hello there");
  await expect(page.locator(".pl-message--user", { hasText: "hello there" })).toBeVisible();

  const menu = await openTabMenu(page);
  const req = handoffRequest(page);
  await menu.getByText("Continue in Zed", { exact: true }).click();
  const body = (await req).postDataJSON();
  expect(body.session_id).toMatch(/^chat-\d+-[a-z0-9]+$/);
  expect(body.project).toBeUndefined();
  expect(body.path).toBeUndefined();
  await expect(
    page.locator(".pl-toast", {
      hasText: "Switch to Zed and start a protoAgent thread within 2 minutes to continue this chat.",
    }),
  ).toBeVisible();
});

test("with a file in the code pane: scopes the hand-off to its project + line", async ({ page }) => {
  await withCodePane(page); // the code pane is an opt-in toolset (ADR 0112 amendment)
  await send(page, "SHOWCODE: show me the auth check");
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");

  const menu = await openTabMenu(page);
  const req = handoffRequest(page);
  await menu.getByText("Continue in Zed", { exact: true }).click();
  const body = (await req).postDataJSON();
  expect(body).toMatchObject({ project: "app", path: "src/server.ts", line: 23 });
  await expect(
    page.locator(".pl-toast", { hasText: "Start a protoAgent thread in Zed within 2 minutes to continue this chat." }),
  ).toBeVisible();
});

test("an unknown session (nothing sent yet) toasts an error", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-handoff": "missing" });
  await page.goto("/app/", { waitUntil: "load" });
  const menu = await openTabMenu(page);
  await menu.getByText("Continue in Zed", { exact: true }).click();
  await expect(page.locator(".pl-toast", { hasText: "Nothing to continue yet" })).toBeVisible();
});

test("not offered when the external editor is not Zed", async ({ page }) => {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("protoagent.editor", "vscode");
    } catch {
      /* ignore */
    }
  });
  await page.goto("/app/", { waitUntil: "load" });
  const menu = await openTabMenu(page);
  await expect(menu.getByText("Export as Markdown", { exact: true })).toBeVisible();
  await expect(menu.getByText("Continue in Zed", { exact: true })).toHaveCount(0);
});

test("not offered on an incognito chat (a Zed thread would continue it without the flag)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  let menu = await openTabMenu(page);
  await menu.getByText("Turn incognito on", { exact: true }).click();
  menu = await openTabMenu(page);
  await expect(menu.getByText("Turn incognito off", { exact: true })).toBeVisible();
  await expect(menu.getByText("Continue in Zed", { exact: true })).toHaveCount(0);
});

test("code pane toolset OFF: the hand-off degrades to the chat alone (no project/file)", async ({ page }) => {
  // show_code's chip renders inert and nothing opens, so there is no pane file to send: the
  // item still works, offering the chat for any folder (ChatSurface reads the pane's
  // `current` only while the toolset is on).
  await send(page, "SHOWCODE: show me the auth check");
  await expect(page.getByTestId("code-ref-inert")).toBeVisible();
  const menu = await openTabMenu(page);
  const req = handoffRequest(page);
  await menu.getByText("Continue in Zed", { exact: true }).click();
  const body = (await req).postDataJSON();
  expect(body.project).toBeUndefined();
  expect(body.path).toBeUndefined();
  await expect(
    page.locator(".pl-toast", {
      hasText: "Switch to Zed and start a protoAgent thread within 2 minutes to continue this chat.",
    }),
  ).toBeVisible();
});

test("the pane's file only rides along for the chat it was opened from", async ({ page }) => {
  await withCodePane(page);
  await send(page, "SHOWCODE: show me the auth check");
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
  const tabs = page.locator(".pl-tabbar__tab");

  // A fresh tab (now the active chat) must NOT send the first chat's file.
  await page.locator(".pl-tabbar__tab").first().click({ button: "right" });
  await page.locator(".pl-menu").getByText("New chat", { exact: true }).click();
  await expect(tabs).toHaveCount(2);
  await tabs.nth(1).click({ button: "right" });
  let req = handoffRequest(page);
  await page.locator(".pl-menu").getByText("Continue in Zed", { exact: true }).click();
  const fresh = (await req).postDataJSON();
  expect(fresh.project).toBeUndefined();
  expect(fresh.path).toBeUndefined();

  // The chat that opened it still scopes to it — even right-clicked from another tab.
  await tabs.first().click({ button: "right" });
  req = handoffRequest(page);
  await page.locator(".pl-menu").getByText("Continue in Zed", { exact: true }).click();
  const origin = (await req).postDataJSON();
  expect(origin).toMatchObject({ project: "app", path: "src/server.ts", line: 23 });
  expect(origin.session_id).not.toBe(fresh.session_id);
});
