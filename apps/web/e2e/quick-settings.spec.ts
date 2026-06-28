import { expect, test } from "@playwright/test";

// Contextual quick-settings + the topbar Settings overlay (ADR 0048): a gear icon
// opens a dialog editing fields via the same /api/settings path, and the central
// two-home one-stop-shop is also openable as an overlay from the topbar.

// The header hamburger opens the app drawer (2026-06-18 IA pass): global actions
// (Settings, Telemetry) + the Docs/Changelog/GitHub links. "Settings" opens the one consolidated
// settings dialog (the same dialog the utility-bar pill opens — Global is no longer a
// separate home).
test("the header hamburger opens the app drawer → Settings dialog", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  const drawer = page.getByTestId("app-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Settings", exact: true })).toBeVisible();
  // The drawer is a single Settings door now (ADR 0048) — no separate Telemetry shortcut.
  await expect(drawer.getByRole("button", { name: "Telemetry", exact: true })).toHaveCount(0);
  await expect(drawer.getByRole("link", { name: "Docs" })).toBeVisible();
  const changelog = drawer.getByRole("link", { name: "Changelog" });
  await expect(changelog).toBeVisible();
  await expect(changelog).toHaveAttribute("href", "https://agent.protolabs.studio/changelog/");
  await expect(changelog).toHaveAttribute("target", "_blank");
  // External link — assert rel guards against tabnabbing (security regression guard).
  await expect(changelog).toHaveAttribute("rel", "noreferrer");
  await expect(drawer.getByRole("link", { name: "GitHub" })).toBeVisible();
  // Footer: version badge + protoLabs.studio branding link.
  await expect(drawer.getByText("v9.9.9", { exact: true })).toBeVisible();
  const built = drawer.getByRole("link", { name: /built by protoLabs\.studio/i });
  await expect(built).toBeVisible();
  await expect(built).toHaveAttribute("href", "https://protolabs.studio");

  await drawer.getByRole("button", { name: "Settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Settings" });
  await expect(dialog).toBeVisible();
  // One consolidated surface — no scope toggle inside (the two-home toggle is gone).
  await expect(dialog.locator(".pl-tabs--segmented")).toHaveCount(0);
});

test("the chat composer model picker overrides the model per-tab (no global save)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // The composer's inline model picker is a PER-TAB override (not a global settings
  // write). It shows the effective model name and offers the gateway's models;
  // picking one stores it on the chat session and is sent with each turn.
  const trigger = page.getByRole("button", { name: "Model for this chat" });
  await expect(trigger).toBeVisible();

  // Picking a model must NOT POST /api/settings (that would change it globally).
  let settingsWrite = false;
  page.on("request", (r) => {
    if (r.url().endsWith("/api/settings") && r.method() === "POST") settingsWrite = true;
  });

  // Open the dropdown and pick a non-default model.
  await trigger.click();
  await page.getByRole("menuitem", { name: "protolabs/fast" }).click();

  // Trigger should now show the selected model name.
  await expect(trigger).toContainText("protolabs/fast");
  await page.waitForTimeout(300);
  expect(settingsWrite).toBe(false);
});
