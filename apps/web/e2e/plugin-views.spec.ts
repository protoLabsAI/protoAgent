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

test("right-click a plugin view → Hide removes its rail icon, and it stays hidden across a reload", async ({ page }) => {
  // ADR 0035/0036 — "hidden but enabled": hiding a plugin view drops its rail icon WITHOUT
  // disabling the plugin, and the reconcile-on-load must NOT resurrect it (the layout-wipe trap).
  await page.goto("/app/", { waitUntil: "load" });
  const rail = page.locator(".pl-rail");
  await expect(rail.getByRole("button", { name: "Board", exact: true })).toBeVisible();

  await rail.getByRole("button", { name: "Board", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Hide", { exact: true }).click();
  await expect(rail.getByRole("button", { name: "Board", exact: true })).toHaveCount(0);
  // Only Board is hidden — its sibling view (Stats) is untouched.
  await expect(rail.getByRole("button", { name: "Stats", exact: true })).toBeVisible();

  // Reload: boardy is still installed (the runtime status lists it), but Board stays hidden —
  // reconcilePluginViews keeps it in the hidden bucket rather than re-docking it.
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").getByRole("button", { name: "Stats", exact: true })).toBeVisible();
  await expect(page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true })).toHaveCount(0);
});

test("right-click the empty rail → a 'Hidden views' menu restores a hidden surface", async ({ page }) => {
  // ADR 0035/0036 — the rail-background menu is the discoverable counterpart to ⌘K for un-hiding.
  await page.goto("/app/", { waitUntil: "load" });
  // The left rail aside (full-height grid column) — by aria-label, so it's unambiguous vs the
  // bottom rail (which also lacks the --right modifier).
  const rail = page.getByRole("complementary", { name: "Left surfaces" });
  await expect(rail.getByRole("button", { name: "Board", exact: true })).toBeVisible();

  // Hide Board first.
  await rail.getByRole("button", { name: "Board", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Hide", { exact: true }).click();
  await expect(rail.getByRole("button", { name: "Board", exact: true })).toHaveCount(0);

  // Right-click the EMPTY rail area (near the bottom of the column, below the icons) → the
  // Hidden views menu lists Board. Compute the point from the rail's box so it's robust to height.
  const box = await rail.boundingBox();
  if (!box) throw new Error("left rail has no bounding box");
  await rail.click({ button: "right", position: { x: box.width / 2, y: box.height - 8 } });
  const menu = page.locator(".pl-menu");
  await expect(menu).toBeVisible();
  await menu.getByText("Board", { exact: true }).click();

  // Board is restored to the rail.
  await expect(rail.getByRole("button", { name: "Board", exact: true })).toBeVisible();
});

test("the Hidden views menu restores onto the rail it was opened on", async ({ page }) => {
  // ADR 0035/0036 — restoring from a rail's background drops the view on THAT dock, not its default.
  await page.goto("/app/", { waitUntil: "load" });
  const leftRail = page.getByRole("complementary", { name: "Left surfaces" });
  const rightRail = page.getByRole("complementary", { name: "Right surfaces" });
  await expect(leftRail.getByRole("button", { name: "Board", exact: true })).toBeVisible();

  // Hide Board (it lives on the left rail by default).
  await leftRail.getByRole("button", { name: "Board", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Hide", { exact: true }).click();
  await expect(leftRail.getByRole("button", { name: "Board", exact: true })).toHaveCount(0);

  // Right-click the RIGHT rail's empty area → restore Board THERE, not back on the left.
  const box = await rightRail.boundingBox();
  if (!box) throw new Error("right rail has no bounding box");
  await rightRail.click({ button: "right", position: { x: box.width / 2, y: box.height - 8 } });
  await page.locator(".pl-menu").getByText("Board", { exact: true }).click();

  await expect(rightRail.getByRole("button", { name: "Board", exact: true })).toBeVisible();
  await expect(leftRail.getByRole("button", { name: "Board", exact: true })).toHaveCount(0);
});

test("right-click the empty rail → 'Manage plugins…' opens Settings ▸ Integrations", async ({ page }) => {
  // The rail-background menu also carries a rail-wide action (not tied to one surface) that opens
  // the plugin manager — Settings ▸ Integrations — via openGlobalSettings("plugins").
  await page.goto("/app/", { waitUntil: "load" });
  const rail = page.getByRole("complementary", { name: "Left surfaces" });
  await expect(rail.getByRole("button", { name: "Board", exact: true })).toBeVisible();

  // Right-click empty rail space (below the icons) → the menu offers "Manage plugins…".
  const box = await rail.boundingBox();
  if (!box) throw new Error("left rail has no bounding box");
  await rail.click({ button: "right", position: { x: box.width / 2, y: box.height - 8 } });
  await page.locator(".pl-menu").getByText("Manage plugins", { exact: false }).click();

  // The one settings dialog opens, deep-linked to the Integrations (plugins) section.
  const overlay = page.locator(".settings-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay.locator(".pl-sidenav__item--active")).toHaveText(/Integrations/);
});

test("right-click a plugin view → Configure opens that plugin's settings dialog", async ({ page }) => {
  // ADR 0036/0059 — a plugin view's rail menu offers "Configure…", which opens the owning
  // plugin's per-plugin settings dialog (titled with the plugin's display name, "Boardy").
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Configure", { exact: false }).click();
  await expect(page.getByRole("dialog", { name: "Boardy" })).toBeVisible();
});

test("right-click a rail icon → 'Manage plugins…' opens Settings ▸ Integrations", async ({ page }) => {
  // The per-icon rail menu also carries the rail-wide "Manage plugins…" action (the all-plugins
  // counterpart to the per-plugin "Configure…"), opening Settings ▸ Integrations.
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Manage plugins", { exact: false }).click();
  const overlay = page.locator(".settings-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay.locator(".pl-sidenav__item--active")).toHaveText(/Integrations/);
});

test("right-click a plugin's util-bar widget → Configure opens its settings dialog", async ({ page }) => {
  // ADR 0036/0059 — the util-bar widget context menu mirrors the rail-icon Configure.
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("util-widget-snap").click({ button: "right" });
  await page.locator(".pl-menu").getByText("Configure", { exact: false }).click();
  await expect(page.getByRole("dialog", { name: "Boardy" })).toBeVisible();
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

  // The hover info popover carries the manifest's `utility.info`. The DS Tooltip is
  // Radix-backed (ui 0.46.0): portaled + shown on hover, so assert the page-level
  // tooltip role after hovering, not a static child of the trigger wrap.
  await pill.hover();
  await expect(page.getByRole("tooltip")).toContainText("A quick board snapshot");

  // Click → the plugin opens in a dialog hosting its iframe at the declared path.
  await pill.click();
  const dialog = page.getByRole("dialog", { name: "Boardy Snapshot" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".plugin-view-frame")).toHaveAttribute("src", /\/plugins\/boardy\/snap/);
});
