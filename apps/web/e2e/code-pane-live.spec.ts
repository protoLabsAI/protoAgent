import { expect, test, type Page } from "@playwright/test";

import { withCodePane } from "./codePane";

// Code pane live updates (ADR 0112). The Diff tab used to sit on "No changes vs HEAD" while
// `@claude-code` edited the project, until the operator clicked Refresh. Now:
//  - an `fs.changed` bus frame (a delegate's or the agent's own write, graph/fs_changes.py)
//    re-fetches the diff, and with Follow on moves the pane to a delegate's edit;
//  - a cheap `/api/fs/stamp` poll catches edits nothing reported (a terminal, an editor).
// The mock's /api/events stream emits `fs.changed` for the `x-e2e-fs-change` header's file.

const CLEAN = { project: "app", is_git: true, head: "a9c0f2d4e1b7", branch: "main", files: [], patch: "" };

async function openDiffTab(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("SHOWCODE: open the pane");
  await composer.press("Enter");
  await expect(page.getByTestId("code-pane")).toBeVisible();
  await page.getByTestId("code-pane").getByRole("tab", { name: "Diff", exact: true }).click();
}

/** The working tree is clean until `state.changed` flips — then the mock's real diff. */
async function routeDiff(page: Page, state: { changed: boolean }) {
  await page.route(/\/api\/fs\/diff\?/, (route) => (state.changed ? route.fallback() : route.fulfill({ json: CLEAN })));
}

test.describe("fs.changed on the bus", () => {
  test.use({ extraHTTPHeaders: { "x-e2e-fs-change": "app:src/server.ts" } });

  test("a file-change event makes the Diff tab show the change — no Refresh click", async ({ page }) => {
    await withCodePane(page);
    const state = { changed: false };
    await routeDiff(page, state);
    // Hold the fallback poll still, so only the bus event can be what refreshes the tab.
    await page.route(/\/api\/fs\/stamp\?/, (route) => route.fulfill({ json: { project: "app", is_git: true, stamp: "same" } }));

    await openDiffTab(page);
    await expect(page.getByTestId("code-pane-clean")).toHaveText("No changes vs HEAD.");

    state.changed = true; // the delegate's edit lands on disk
    const list = page.getByTestId("code-pane-files");
    await expect(list.locator("button").first()).toContainText("src/server.ts", { timeout: 5_000 });
    await expect(page.getByTestId("code-pane-clean")).toHaveCount(0);
  });
});

test.describe("follow mode", () => {
  test.use({ extraHTTPHeaders: { "x-e2e-fs-change": "app:notes/todo.md;delegate" } });

  test("Follow on: a delegate's edit switches the pane to that file's diff", async ({ page }) => {
    await withCodePane(page);
    await page.goto("/app/", { waitUntil: "load" });
    const composer = page.getByPlaceholder(/Message protoAgent/i);
    await composer.waitFor({ state: "visible" });
    await composer.fill("SHOWCODE: open the pane");
    await composer.press("Enter");
    await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
    const pane = page.getByTestId("code-pane");
    await expect(pane.getByRole("tab", { name: "File", exact: true, selected: true })).toBeVisible();

    await page.getByTestId("code-follow").click();
    await expect(pane.getByRole("tab", { name: "Diff", exact: true, selected: true })).toBeVisible({ timeout: 5_000 });
    await expect(page.locator(".code-pane__file-row.is-active")).toContainText("notes/todo.md");
    const diffHost = page.locator(".code-pane__body--diff diffs-container");
    await expect
      .poll(() => diffHost.evaluate((el) => el.shadowRoot?.textContent?.includes("rate-limit") ?? false))
      .toBe(true);
  });
});

test("the fallback poll: an edit nothing reported still shows up", async ({ page }) => {
  await withCodePane(page);
  const state = { changed: false };
  await routeDiff(page, state);
  await page.route(/\/api\/fs\/stamp\?/, (route) =>
    route.fulfill({ json: { project: "app", is_git: true, stamp: state.changed ? "after" : "before" } }),
  );

  await openDiffTab(page);
  await expect(page.getByTestId("code-pane-clean")).toBeVisible();
  state.changed = true; // edited in a terminal: no fs.changed, only the stamp moves
  await expect(page.getByTestId("code-pane-files").locator("button").first()).toContainText("src/server.ts", {
    timeout: 6_000,
  });
});
