import { expect, test } from "@playwright/test";

// The Knowledge ▸ Playbooks surface (ADR 0009) browses the skill index:
// pinned (SKILL.md) vs learned (agent-emitted), with search + delete-with-confirm.

test("Knowledge → Playbooks lists pinned + learned skills and supports search", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();
  // Knowledge lands on Store now (ADR 0020) — switch to the Playbooks sub-tab.
  await page.locator(".stage-subnav").getByRole("button", { name: "Skills", exact: true }).click();

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
  await page.getByRole("button", { name: "Knowledge" }).click();
  await page.locator(".stage-subnav").getByRole("button", { name: "Skills", exact: true }).click();
  const surface = page.getByTestId("playbooks-surface");
  await expect(surface).toBeVisible();

  // Delete the learned one → custom confirm dialog (not window.confirm).
  await surface.getByTestId("playbook-delete-2").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();

  // Cancel keeps it.
  await page.getByTestId("confirm-cancel").click();
  await expect(surface.getByText("pr-triage-flow")).toBeVisible();

  // Confirm removes the row.
  await surface.getByTestId("playbook-delete-2").click();
  await page.getByTestId("confirm-accept").click();
  await expect(surface.getByText("pr-triage-flow")).toBeHidden();
});
