import { expect, test } from "@playwright/test";

// Plugin-contributed console surfaces (ADR 0026): an enabled plugin that declares
// a `views` entry (surfaced via /api/runtime/status) gets a dynamic rail icon
// whose panel is an iframe of the page the plugin serves. The mock runtime-status
// includes a "boardy" plugin with one view.

test("a plugin view adds a rail icon that opens its page in an iframe", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // The plugin's view label appears as a rail button (beyond the core surfaces).
  const railBtn = page.locator(".rail").getByRole("button", { name: "Board", exact: true });
  await expect(railBtn).toBeVisible();

  // Clicking it hosts the plugin page in a same-origin iframe at the declared path.
  await railBtn.click();
  const frame = page.locator(".plugin-view-frame");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/board/);
  await expect(frame).toHaveAttribute("sandbox", /allow-scripts/);

  // Switching back to a core surface (Chat) hides the plugin view.
  await page.locator(".rail").getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveCount(0);
});

test("switches between two plugin views, each loading its own page", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rail = page.locator(".rail");
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
  await page.locator(".rail").getByRole("button", { name: "Board", exact: true }).click();
  const subnav = page.locator(".stage-subnav");
  await expect(subnav.getByRole("button", { name: "Open", exact: true })).toBeVisible();
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /tab=open/);
  await subnav.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /tab=done/);
});

test("console hands the plugin view a bearer + theme via postMessage", async ({ page }) => {
  // Seed an operator token so the console forwards it post-load.
  await page.addInitScript(() => window.localStorage.setItem("protoagent.authToken", "e2e-token"));
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".rail").getByRole("button", { name: "Stats", exact: true }).click();
  // The plugin page flips data-bridge on receiving protoagent:init with a token.
  const body = page.frameLocator(".plugin-view-frame").locator("body");
  await expect(body).toHaveAttribute("data-bridge", "authed");
});

test("a plugin view with placement:right becomes a right-sidebar panel", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // The right-placed view is a right-rail tab (not a left-rail surface icon).
  const tab = page.locator(".rail-right").getByRole("button", { name: "Scratch", exact: true });
  await expect(tab).toBeVisible();
  await tab.click();

  // It hosts the plugin page in the same iframe host, at the declared path.
  const frame = page.locator(".plugin-view-frame");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/scratch/);
});

test("a ui:react view mounts a federated React remote (ADR 0034), not an iframe", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Right-panel tab for the React view.
  await page.locator(".rail-right").getByRole("button", { name: "React Panel", exact: true }).click();

  // The federated remote mounts into the host React tree — its content renders directly
  // (no iframe). If React were dual-loaded, the remote's hook would throw on render.
  await expect(page.getByText("Hello from a React plugin remote", { exact: false })).toBeVisible();
  await expect(page.locator(".plugin-view-frame")).toHaveCount(0);

  // The shared-React hook works: clicking increments the remote's useState counter.
  await page.getByRole("button", { name: /clicked 0×/ }).click();
  await expect(page.getByRole("button", { name: /clicked 1×/ })).toBeVisible();
});

test("a ui:react remote contributes a context-menu item via the SDK (ADR 0034 S2 / 0036)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Open the React view → the remote mounts and registers a menu item through @protoagent/plugin-ui.
  await page.locator(".rail-right").getByRole("button", { name: "React Panel", exact: true }).click();
  await expect(page.getByText("Hello from a React plugin remote", { exact: false })).toBeVisible();

  // Right-click a rail surface → the HOST's menu now includes the PLUGIN's item, proving a remote
  // registers into the host's shared registry across the federation boundary.
  await page.locator(".rail-right").getByRole("button", { name: "Notes", exact: true }).click({ button: "right" });
  await expect(
    page.getByTestId("context-menu").getByText("Hello from the React plugin", { exact: false }),
  ).toBeVisible();
});

test("the trust gate degrades an untrusted ui:react view to an iframe (ADR 0034 D5)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".rail-right").getByRole("button", { name: "React Untrusted", exact: true }).click();
  // Untrusted ui:react → the sandboxed iframe of its path, NOT the in-process federated remote.
  await expect(page.locator(".plugin-view-frame")).toBeVisible();
  await expect(page.getByText("Hello from a React plugin remote", { exact: false })).toHaveCount(0);
});
