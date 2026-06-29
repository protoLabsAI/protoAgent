import { expect, test } from "@playwright/test";

// The Knowledge ▸ Playbooks surface (ADR 0009) browses the skill index:
// pinned (SKILL.md) vs learned (agent-emitted), with search + delete-with-confirm.

// The promote test MUTATES the shared mock (a skill flips private→commons) and a
// commons skill is read-only under the CRUD editability gating, so its delete
// affordance is gone. With fullyParallel the file's tests would otherwise run on
// separate workers against the one mock process and stomp each other (the promote
// leaks into the delete test). Run serially + reset the mock before every test —
// same guard the fleet spec uses for its shared mutable state.
test.describe.configure({ mode: "serial" });
test.beforeEach(async ({ page }) => {
  await page.request.post("/api/__test__/playbooks/reset");
});

test("Agent → Skills lists pinned + learned skills and supports search", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
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

test("the + button opens the New skill DIALOG, not an inline panel form", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Skills", exact: true }).click();
  const surface = page.getByTestId("playbooks-surface");
  await expect(surface).toBeVisible();

  // Closed: the form isn't anywhere yet.
  await expect(surface.getByLabel("skill name")).toHaveCount(0);

  await page.getByTestId("playbook-new").click();
  // It opens as a MODAL dialog (role=dialog) — an inline panel form wouldn't be one.
  const dialog = page.getByRole("dialog", { name: "New skill" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("skill name")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Create skill" })).toBeVisible();

  // The "user only" (hide from the agent) toggle appears only once it's a /slash command.
  const userOnly = dialog.getByLabel(/hide from the agent/i);
  await expect(userOnly).toHaveCount(0);
  await dialog.getByLabel("invokable as a slash command").check();
  await expect(userOnly).toBeVisible();
});

test("layered skills show tier badges and promote a private skill to the commons", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
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

test("unshares a commons skill from the Skills view (forget, with confirm)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Skills", exact: true }).click();
  const surface = page.getByTestId("playbooks-surface");
  await expect(surface).toBeVisible();

  // Unshare is offered on the commons skill (id 1), not the private one (id 2).
  await expect(surface.getByTestId("playbook-forget-1")).toBeVisible();
  await expect(surface.getByTestId("playbook-forget-2")).toHaveCount(0);

  // It confirms first (a commons skill is read by every agent), then removes the row.
  await surface.getByTestId("playbook-forget-1").click();
  const dialog = page.getByRole("dialog", { name: "Unshare from the commons?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Unshare", exact: true }).click();
  await expect(surface.getByTestId("playbook-forget-1")).toHaveCount(0);
});

test("deleting a playbook confirms first, then removes it", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
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

test("a failed skills load surfaces a toast (errors are no longer swallowed)", async ({ page }) => {
  // PlaybooksSurface is embedded in Settings ▸ Skills with no error host; it used to delegate
  // failures to a no-op onError prop and swallow them. It now self-reports via toast.
  await page.route("**/api/playbooks", (route) =>
    route.request().method() === "GET"
      ? route.fulfill({ status: 500, json: { detail: "boom" } })
      : route.fallback(),
  );
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Skills", exact: true }).click();
  await expect(page.locator(".pl-toast", { hasText: /Skills/i })).toBeVisible();
});
