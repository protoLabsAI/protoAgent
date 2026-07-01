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

test("Shift+click deletes a chunk immediately, plain click still confirms (#1582)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();
  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible();

  const del12 = surface.getByLabel("delete entry 12", { exact: true });
  await expect(del12).toBeVisible();

  // Plain click → the confirmation dialog (safe path preserved). Back out; chunk stays.
  await del12.click();
  const confirm = page.getByRole("dialog", { name: "Delete this knowledge entry?" });
  await expect(confirm).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(confirm).toHaveCount(0);
  await expect(del12).toBeVisible();

  // Shift+click → no dialog, chunk removed immediately (chat-tab quick-delete parity).
  await del12.click({ modifiers: ["Shift"] });
  await expect(page.getByRole("dialog", { name: "Delete this knowledge entry?" })).toHaveCount(0);
  await expect(surface.getByLabel("delete entry 12", { exact: true })).toHaveCount(0);
});

test("groups a multi-chunk source into a collapsible section (#1575)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();
  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible();

  // The 3-chunk YouTube source collapses under one header (title + count), closed by default.
  const header = surface.getByRole("button", { name: /Hiking with Kevin/ });
  await expect(header).toBeVisible();
  await expect(header).toContainText("3 chunks");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await expect(surface.getByText("Switchbacks keep the grade walkable.")).toHaveCount(0); // hidden while collapsed

  // Loose chunks (single/no source) still render flat — no regression.
  await expect(surface.getByText("Releases are cut manually via workflow_dispatch.")).toBeVisible();

  // Clicking the header expands it → the chunks render; clicking again collapses.
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(surface.getByText("Switchbacks keep the grade walkable.")).toBeVisible();
  await expect(surface.getByText("The summit view pays off the climb.")).toBeVisible();
  await header.click();
  await expect(surface.getByText("Switchbacks keep the grade walkable.")).toHaveCount(0);
});
