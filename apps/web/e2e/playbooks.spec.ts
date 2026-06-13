import { expect, test } from "@playwright/test";

// The Knowledge ▸ Playbooks surface (ADR 0009) browses the skill index:
// pinned (SKILL.md) vs learned (agent-emitted), with search + delete-with-confirm.

test("Agent → Skills lists pinned + learned skills and supports search", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Skills", exact: true }).click();

  const surface = page.getByTestId("playbooks-surface");
  await expect(surface).toBeVisible();

  // Both fixtures render with their source badges.
  await expect(surface.getByText("web-research")).toBeVisible();
  await expect(surface.getByText("pr-triage-flow")).toBeVisible();
  await expect(surface.getByText("pinned").first()).toBeVisible();
  await expect(surface.getByText("learned").first()).toBeVisible();

  // Search narrows the list.
  await surface.getByPlaceholder(/Search skills/).fill("triage");
  await expect(surface.getByText("pr-triage-flow")).toBeVisible();
  await expect(surface.getByText("web-research")).toBeHidden();
});

test("layered skills show tier badges and promote a private skill to the commons", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Skills", exact: true }).click();
  const surface = page.getByTestId("playbooks-surface");
  await expect(surface).toBeVisible();

  // Tier badges from the layered index (ADR 0041): one commons, one private.
  await expect(surface.getByText("commons", { exact: true })).toBeVisible();
  await expect(surface.getByText("private", { exact: true })).toBeVisible();

  // Promote is offered only on the private skill (id 2), not the commons one (id 1).
  await expect(surface.getByTestId("playbook-promote-2")).toBeVisible();
  await expect(surface.getByTestId("playbook-promote-1")).toHaveCount(0);

  // Promoting lifts it into the commons → the button is gone afterward.
  await surface.getByTestId("playbook-promote-2").click();
  await expect(surface.getByTestId("playbook-promote-2")).toHaveCount(0);
});

test("deleting a playbook confirms first, then removes it", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Skills", exact: true }).click();
  const surface = page.getByTestId("playbooks-surface");
  await expect(surface).toBeVisible();

  // Delete the learned one → confirm dialog (@protolabsai/ui, not window.confirm).
  await surface.getByTestId("playbook-delete-2").click();
  const dialog = page.getByRole("dialog", { name: "Delete skill?" });
  await expect(dialog).toBeVisible();

  // Cancel keeps it.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(surface.getByText("pr-triage-flow")).toBeVisible();

  // Confirm removes the row.
  await surface.getByTestId("playbook-delete-2").click();
  await dialog.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(surface.getByText("pr-triage-flow")).toBeHidden();
});
