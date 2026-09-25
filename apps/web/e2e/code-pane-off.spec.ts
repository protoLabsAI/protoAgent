import { expect, test, type Page } from "@playwright/test";

import { withCodePane } from "./codePane";
import { expandToolCard } from "./toolcard";

// The code pane is an opt-in toolset (ADR 0112 amendment, `filesystem.code_pane`, default
// OFF). With it off — the mock's default, like the server's — the console has no Code
// surface, no "protoAgent" choice under Settings ▸ Chat ▸ Open files in, file paths in tool
// output link to the external editor (Zed by default, as before the pane), and a code-ref
// from the transcript renders as inert text. Turning it on in Tools lights it all up in place.

async function send(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

const codeRailButton = (page: Page) => page.locator(".pl-rail").getByRole("button", { name: "Code", exact: true });

test("off: no Code surface, a code-ref renders inert, and nothing opens", async ({ page }) => {
  await send(page, "SHOWCODE: show me the auth check");
  const inert = page.getByTestId("code-ref-inert");
  await expect(inert).toBeVisible();
  await expect(inert).toContainText("app/src/server.ts:23-29");
  await expect(inert).toContainText("constant-time");
  await expect(page.getByTestId("code-ref-chip")).toHaveCount(0);
  await page.waitForTimeout(300);
  await expect(page.getByTestId("code-pane")).toHaveCount(0);
  await expect(codeRailButton(page)).toHaveCount(0);
});

test("off: a read_file path links to the external editor (Zed by default)", async ({ page }) => {
  // A stored "protoagent" choice (the pane-era default) must fall back, not dead-end.
  await page.addInitScript(() => localStorage.setItem("protoagent.openFilesIn", "protoagent"));
  await send(page, "READFILE: look at the handler");
  const card = page.locator(".pl-toolcard").first();
  await expect(card).toBeVisible();
  await expandToolCard(page, card);
  const link = page.locator("a.tool-editor-link").first();
  await expect(link).toHaveText("src/server.ts:34");
  await expect(link).toHaveAttribute("href", "zed://file/home/op/dev/app/src/server.ts:34");
  await expect(link).toHaveAttribute("title", "Open in Zed");
});

test("off: Settings ▸ Chat ▸ Open files in offers no protoAgent choice", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Chat", exact: true }).click();
  const select = page.locator("#chat-open-files-in");
  await expect(select).toContainText("Zed");
  await select.click();
  await expect(page.getByRole("menuitemradio", { name: "Zed" })).toBeVisible();
  await expect(page.getByRole("menuitemradio", { name: /protoAgent/ })).toHaveCount(0);
  await expect(page.locator('.setting-row[data-key="chat.externalEditor"]')).toHaveCount(0);
});

test("on: turning the Code pane on in Tools lights the surface up without a reload", async ({ page }) => {
  const state = await withCodePane(page, { enabled: false });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await expect(codeRailButton(page)).toHaveCount(0);

  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Tools", exact: true }).click();
  await page.getByRole("button", { name: /Filesystem/ }).click();
  await page.getByRole("button", { name: "Shell & filesystem tools" }).click();
  const dialog = page.getByRole("dialog", { name: "Shell & filesystem tools" });
  await expect(dialog.getByText("Code pane", { exact: true })).toBeVisible();
  const saved = page.waitForRequest((r) => r.url().endsWith("/api/settings") && ["POST", "PUT"].includes(r.method()));
  await dialog.locator('[data-key="filesystem.code_pane"] .pl-switch').click();
  // The server applies the save (hot reload); the next status read reports the pane on.
  state.enabled = true;
  await dialog.getByRole("button", { name: "Save" }).click();
  expect((await saved).postDataJSON().updates["filesystem.code_pane"]).toBe(true);

  await page.locator(".settings-overlay .pl-dialog__close").click();
  await expect(codeRailButton(page)).toBeVisible();
});
