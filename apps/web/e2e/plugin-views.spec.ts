import { expect, test } from "@playwright/test";

// Plugin-contributed console surfaces (ADR 0026): an enabled plugin that declares
// a `views` entry (surfaced via /api/runtime/status) gets a dynamic rail icon
// whose panel is an iframe of the page the plugin serves. The mock runtime-status
// includes a "boardy" plugin with one view.

test("a plugin view adds a rail icon that opens its page in an iframe", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // The plugin's view label appears as a rail button (beyond the core surfaces).
  const railBtn = page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true });
  await expect(railBtn).toBeVisible();

  // Clicking it hosts the plugin page in a same-origin iframe at the declared path.
  await railBtn.click();
  const frame = page.locator(".plugin-view-frame");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/board/);
  await expect(frame).toHaveAttribute("sandbox", /allow-scripts/);

  // Switching back to a core surface (Chat) hides the plugin view.
  await page.locator(".pl-rail").getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveCount(0);
});

test("switches between two plugin views, each loading its own page", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rail = page.locator(".pl-rail");
  const frame = page.locator(".plugin-view-frame");

  await rail.getByRole("button", { name: "Board", exact: true }).click();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/board/);

  await rail.getByRole("button", { name: "Stats", exact: true }).click();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/stats/);

  // exactly one plugin view is shown at a time
  await expect(frame).toHaveCount(1);
});

test("view-tabs switch the hosted page", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true }).click();
  const subnav = page.locator(".pl-tabs");
  await expect(subnav.getByRole("tab", { name: "Open", exact: true })).toBeVisible();
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /tab=open/);
  await subnav.getByRole("tab", { name: "Done", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /tab=done/);
});

test("console hands the plugin view a bearer + theme via postMessage", async ({ page }) => {
  // Seed an operator token so the console forwards it post-load.
  await page.addInitScript(() => window.localStorage.setItem("protoagent.authToken", "e2e-token"));
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Stats", exact: true }).click();
  // The plugin page flips data-bridge on receiving protoagent:init with a token.
  const body = page.frameLocator(".plugin-view-frame").locator("body");
  await expect(body).toHaveAttribute("data-bridge", "authed");
});

test("a plugin bus event lights the rail notification dot, cleared on open", async ({ page }) => {
  // ADR 0039 — the mock pushes a `boardy.created` event on the /api/events stream; the
  // console routes it by topic and lights the boardy surface's rail icon until it's opened.
  await page.goto("/app/", { waitUntil: "load" });
  const board = page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true });
  await expect(board).toBeVisible();
  await expect(board.locator(".pl-rail__dot")).toBeVisible(); // event arrived → dot

  await board.click(); // opening the surface clears its dot
  await expect(board.locator(".pl-rail__dot")).toHaveCount(0);
});

test("a COLLAPSED right panel still lights its plugin's dot (collapsed ≠ visible)", async ({ page }) => {
  // Regression: a right-placed plugin selected as the right panel but COLLAPSED must not count as
  // "visible" — otherwise its dot is suppressed + cleared forever (persisted), so it can never ping.
  await page.addInitScript(() => {
    localStorage.setItem(
      "protoagent.ui",
      JSON.stringify({ state: { rightCollapsed: true, rightPanel: "plugin:boardy:scratch" }, version: 2 }),
    );
  });
  await page.goto("/app/", { waitUntil: "load" });
  // The mock streams `boardy.created`; Scratch is selected but collapsed → its rail icon dots.
  const scratch = page.locator(".pl-rail--right").getByRole("button", { name: "Scratch", exact: true });
  await expect(scratch.locator(".pl-rail__dot")).toBeVisible();
});

test("a 404-ing plugin view shows an actionable error, not a blank panel", async ({ page }) => {
  // P0 regression: a same-origin 404 fires the iframe's onLoad (not onError), so trusting
  // onLoad rendered the server's bare {"detail":"Not Found"} as the "view" (the blank
  // "no details" panel). The host now status-PROBES the route and surfaces a real error.
  await page.route("**/plugins/boardy/stats", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"Not Found"}' }),
  );
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Stats", exact: true }).click();

  // No iframe is mounted for an unreachable route; an actionable alert is shown instead.
  await expect(page.locator(".plugin-view [role=alert]")).toContainText("Couldn’t load");
  await expect(page.locator(".plugin-view-frame")).toHaveCount(0);
});

test("a plugin view with placement:right becomes a right-sidebar panel", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // The right-placed view is a right-rail tab (not a left-rail surface icon).
  const tab = page.locator(".pl-rail--right").getByRole("button", { name: "Scratch", exact: true });
  await expect(tab).toBeVisible();
  await tab.click();

  // It hosts the plugin page in the same iframe host, at the declared path.
  const frame = page.locator(".plugin-view-frame");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/scratch/);
});

test("a plugin view with utility:{...} is a bottom-left widget (hover info + click dialog)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // It's a pill in the bottom-left widgets cluster — NOT a left-rail surface.
  const pill = page.getByTestId("util-widget-snap");
  await expect(pill).toBeVisible();
  await expect(page.locator(".pl-rail").getByRole("button", { name: "Boardy Snapshot" })).toHaveCount(0);

  // The hover info popover carries the manifest's `utility.info`.
  await expect(page.locator(".pl-tip-wrap", { has: pill }).getByRole("tooltip")).toContainText("A quick board snapshot");

  // Click → the plugin opens in a dialog hosting its iframe at the declared path.
  await pill.click();
  const dialog = page.getByRole("dialog", { name: "Boardy Snapshot" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".plugin-view-frame")).toHaveAttribute("src", /\/plugins\/boardy\/snap/);
});
