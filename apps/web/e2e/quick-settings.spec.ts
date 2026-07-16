import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

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

test("Tools panel: the Filesystem group's inline settings disable run_command via /api/settings", async ({ page }) => {
  // The per-agent run_command kill switch is config for ONE tool group, so it opens in
  // place inside that group (#2000) rather than in a dialog over the panel. Same
  // /api/settings write path as the central home; writes on change like the tool rows.
  await openToolsTab(page);

  // The Filesystem group isn't the default-open one (that's General) — open it.
  await page.getByRole("button", { name: /^Filesystem/ }).click();
  await page.getByRole("button", { name: "Filesystem settings" }).click();

  const panel = page.locator(".tools-group-settings--open");
  await expect(panel).toBeVisible();
  // No modal: the settings live in the group. (The Settings OVERLAY hosting this panel is
  // itself role=dialog, so this asserts the absence of the old QuickSetting one by name.)
  await expect(page.getByRole("dialog", { name: "Shell & filesystem tools" })).toHaveCount(0);
  await expect(panel.getByText("Allow run_command")).toBeVisible();

  const saved = page.waitForRequest(
    (r) => r.url().endsWith("/api/settings") && ["POST", "PUT"].includes(r.method()),
  );
  // Flipping the switch writes immediately — there is no Save button to press.
  await panel.locator('[data-key="filesystem.allow_run"] .pl-switch').click();
  const req = await saved;
  const body = req.postDataJSON();
  expect(body.updates["filesystem.allow_run"]).toBe(false);
  expect(body.layer ?? "agent").toBe("agent"); // per-agent leaf, not box-wide
  await expect(page.locator(".pl-toast").getByText("Saved")).toBeVisible();

  // depends_on: with run_command off, its dependent gates govern nothing and hide —
  // the dialog used to render them regardless.
  await expect(panel.getByText("Require approval per command")).toHaveCount(0);
});

// The old "Disabled tools" chip (a raw tools.disabled textarea) is gone — every row
// in the list carries an on/off switch writing the same denylist. These specs pin the
// row-toggle contract: the POST edits the RAW denylist (preserving entries it didn't
// touch, incl. stale names with no live tool) and off rows stay listed.
async function openToolsTab(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .locator(".settings-overlay .pl-sidenav")
    .getByRole("tab", { name: "Tools", exact: true })
    .click();
}

test("Tools panel: the chip strip is gone — settings live on the group they govern", async ({ page }) => {
  await openToolsTab(page);
  // The old "Disabled tools" chip went when every row grew a switch; the "Shell &
  // filesystem tools" chip went in #2000 when its settings moved into the Filesystem group.
  await expect(page.getByRole("button", { name: "Disabled tools" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Shell & filesystem tools" })).toHaveCount(0);
  // Groups without settings don't grow a gear — General is open by default.
  await expect(page.getByRole("button", { name: "General settings" })).toHaveCount(0);
});

test("Tools panel: toggling a tool row off appends it to tools.disabled", async ({ page }) => {
  await openToolsTab(page);

  // web_search sits in General — the first group, expanded by default.
  const row = page.locator(".tools-row", { has: page.getByText("web_search", { exact: true }) });
  await expect(row).toBeVisible();

  const saved = page.waitForRequest(
    (r) => r.url().endsWith("/api/settings") && ["POST", "PUT"].includes(r.method()),
  );
  await row.locator(".pl-switch").click();
  const req = await saved;
  const body = req.postDataJSON();
  // Appends the toggled name and PRESERVES the rest of the raw denylist — including
  // ghost_tool, a stale entry with no live tool row to recompute it from.
  expect(body.updates["tools.disabled"]).toEqual(["run_command", "ghost_tool", "web_search"]);
  expect(body.layer ?? "agent").toBe("agent"); // per-agent leaf, not box-wide
});

test("Tools panel: an off tool stays listed and toggles back on", async ({ page }) => {
  await openToolsTab(page);

  // run_command ships disabled in the fixture — still listed (dimmed) under Filesystem.
  await page.locator(".pl-accordion__trigger", { hasText: "Filesystem" }).click();
  const row = page.locator(".tools-row", { has: page.getByText("run_command", { exact: true }) });
  await expect(row).toBeVisible();
  await expect(row).toHaveClass(/tools-row--off/);

  const saved = page.waitForRequest(
    (r) => r.url().endsWith("/api/settings") && ["POST", "PUT"].includes(r.method()),
  );
  await row.locator(".pl-switch").click();
  const req = await saved;
  // Re-enabling removes ONLY run_command; the stale ghost_tool entry survives.
  expect(req.postDataJSON().updates["tools.disabled"]).toEqual(["ghost_tool"]);
});
