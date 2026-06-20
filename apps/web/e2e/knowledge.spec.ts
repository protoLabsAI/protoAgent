import { expect, test } from "@playwright/test";

// Knowledge: a searchable window onto the agent's knowledge base (findings,
// notes, daily-log). A single panel — Skills moved to the Agent section.

// The promote/unshare spec MUTATES the shared mock (a chunk flips private→commons,
// then a commons chunk is dropped). Run serially + reset before each test so it
// doesn't leak into the list test — same guard the playbooks spec uses.
test.describe.configure({ mode: "serial" });
test.beforeEach(async ({ page }) => {
  await page.request.post("/api/__test__/knowledge/reset");
});

test("Knowledge lands on the searchable Store and lists chunks", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();

  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible(); // single Store panel
  await expect(surface.getByRole("heading", { name: "Knowledge" })).toBeVisible();

  // The mocked chunks render with their content + domain badges.
  await expect(surface.getByText("Releases are cut manually via workflow_dispatch.")).toBeVisible();
  await expect(surface.getByText("protolabs/reasoning", { exact: false })).toBeVisible();
  await expect(surface.getByText("process", { exact: true })).toBeVisible(); // domain badge

  // The search box is present (server-side FTS; the mock returns the fixture).
  await expect(surface.getByPlaceholder(/Search the knowledge base/)).toBeVisible();
});

test("layered knowledge shows tier badges and shares / unshares the commons", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();
  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible();

  // Tier badges from the layered store (ADR 0041): one commons, one private.
  await expect(surface.getByText("commons", { exact: true })).toBeVisible();
  await expect(surface.getByText("private", { exact: true })).toBeVisible();

  // Share is offered on the private chunk (12); the commons chunk (11) offers Unshare.
  // (exact: true — "share entry 11" is a substring of "unshare entry 11".)
  await expect(surface.getByLabel("share entry 12", { exact: true })).toBeVisible();
  await expect(surface.getByLabel("unshare entry 11", { exact: true })).toBeVisible();
  await expect(surface.getByLabel("share entry 11", { exact: true })).toHaveCount(0);

  // Unshare the commons chunk → confirm → it leaves the commons.
  await surface.getByLabel("unshare entry 11", { exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Unshare from the commons?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Unshare", exact: true }).click();
  await expect(surface.getByLabel("unshare entry 11", { exact: true })).toHaveCount(0);
});
