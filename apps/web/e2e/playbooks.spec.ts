import { expect, test } from "@playwright/test";

// The Knowledge ▸ Playbooks surface (ADR 0009) browses the skill index:
// pinned (SKILL.md) vs learned (agent-emitted), with search + delete-with-confirm.

test("Agent → Skills lists pinned + learned skills and supports search", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Agent", exact: true }).click();
  // Skills moved under the Agent section — switch to the Skills tab.
  await page.locator(".pl-tabs").getByRole("tab", { name: "Skills", exact: true }).click();

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

test("deleting a playbook confirms first, then removes it", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Agent", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Skills", exact: true }).click();
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
