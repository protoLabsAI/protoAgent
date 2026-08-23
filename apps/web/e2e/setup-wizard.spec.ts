import { expect, test } from "@playwright/test";

import { HARD_GATE_HINT, PM_CONTRACT_NOTICE } from "./fixtures.mjs";

// The first-run Setup Wizard (host path, ADR 0100) — the archetype picker's hard gate
// (#2977/#2979/#2984) mirrored from the fleet New-agent panel: a required bundle
// `config_inputs` answer has no env fallback, so Finish stays disabled until it's
// answered, the answers ride the bundle install even when Configure is collapsed, and
// the collapsed state still explains the disabled button.
//
// The mock reports setup_complete:true for every other spec; these flip it per-test so
// the wizard mounts over the shell. Finishing never flips it back (the override is
// sticky), which keeps the wizard open for the post-Finish payload assertions.

async function openWizard(page) {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.setup_complete = false;
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });
  const wizard = page.getByRole("dialog", { name: "Setup" });
  await expect(wizard).toBeVisible();
  // welcome → agent (name & persona)
  await wizard.getByRole("button", { name: "Next" }).click();
  await expect(wizard.getByLabel("Agent name")).toBeVisible();
  return wizard;
}

// Pick the (advanced, collapsed) Project Manager persona; Configure opens with its fields.
async function pickProjectManager(wizard) {
  await wizard.getByRole("button", { name: /^Advanced \(1\)/ }).click();
  await wizard.locator(".pl-radiocard", { hasText: "Project Manager" }).click();
  await expect(wizard.getByLabel("Repository path")).toBeVisible();
}

// agent → brain → finish. The Brain step's Next needs a gateway base + model, which the
// wizard hydrates with defaults from the mock's /api/config.
async function goToFinish(wizard) {
  await wizard.getByRole("button", { name: "Next" }).click();
  await expect(wizard.getByRole("button", { name: "Next" })).toBeEnabled();
  await wizard.getByRole("button", { name: "Next" }).click();
  await expect(wizard.getByRole("button", { name: "Finish" })).toBeVisible();
}

const WIZARD_HARD_GATE_HINT = HARD_GATE_HINT.replace("this agent can be created", "setup can finish");
const coderTrigger = (wizard) => wizard.locator('[id="config:project_board.coder"]');

test("picking Project Manager shows its capability contract and the required-answer hint", async ({ page }) => {
  const wizard = await openWizard(page);
  await pickProjectManager(wizard);
  await expect(wizard.getByRole("note").filter({ hasText: PM_CONTRACT_NOTICE })).toBeVisible();
  await expect(wizard.getByRole("button", { name: /Configure Project Manager/ })).toContainText("answers marked * are required");
  await expect(wizard.getByText(WIZARD_HARD_GATE_HINT, { exact: true })).toBeVisible();
});

test("Finish stays disabled while a required bundle answer is blank (#2977)", async ({ page }) => {
  const wizard = await openWizard(page);
  await pickProjectManager(wizard);
  // One of the two hard-required answers filled — still gated.
  await wizard.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await goToFinish(wizard);
  await expect(wizard.getByRole("button", { name: "Finish" })).toBeDisabled();
});

test("collapsing Configure with a required answer blank keeps the explanation visible (#2979)", async ({ page }) => {
  const wizard = await openWizard(page);
  await pickProjectManager(wizard);
  const toggle = wizard.getByRole("button", { name: /Configure Project Manager/ });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(wizard.getByLabel("Repository path")).toHaveCount(0);
  await expect(wizard.getByText(/Fields marked \* are needed before setup can finish — open Configure\./)).toBeVisible();
});

test("both required answers enable Finish; config_inputs ride the bundle install even after collapsing Configure (#2979)", async ({ page }) => {
  const wizard = await openWizard(page);
  let installed = null;
  await page.route("**/api/plugins/install", async (route) => {
    if (route.request().method() === "POST") installed = route.request().postDataJSON();
    return route.continue();
  });
  await pickProjectManager(wizard);
  await wizard.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await coderTrigger(wizard).click();
  // Only acp delegates are offered as the coder (#2934) — the a2a peer and the openai
  // endpoint from /api/delegates are filtered out.
  await expect(page.getByRole("menuitemradio", { name: "coder", exact: true })).toBeVisible();
  await expect(page.getByRole("menuitemradio", { name: "peer-pm", exact: true })).toHaveCount(0);
  await expect(page.getByRole("menuitemradio", { name: "opus", exact: true })).toHaveCount(0);
  await page.getByRole("menuitemradio", { name: "coder", exact: true }).click();
  await expect(wizard.getByText(WIZARD_HARD_GATE_HINT, { exact: true })).toHaveCount(0);

  // The fill-then-collapse regression: answers collected, section collapsed, they must
  // still reach the install (the host refuses to activate the bundle without them).
  await wizard.getByRole("button", { name: /Configure Project Manager/ }).click();
  await expect(wizard.getByLabel("Repository path")).toHaveCount(0);

  await goToFinish(wizard);
  const finish = wizard.getByRole("button", { name: "Finish" });
  await expect(finish).toBeEnabled();
  await finish.click();

  await expect.poll(() => installed).not.toBeNull();
  expect(installed?.url).toBe("https://github.com/protoLabsAI/project-manager-archetype");
  expect(installed?.config_inputs).toEqual({ "project_board.repo": "/Users/me/dev/repo", "project_board.coder": "coder" });
});
