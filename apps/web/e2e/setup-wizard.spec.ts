import { expect, test } from "@playwright/test";

import { requiresToolsNotice } from "../src/lib/archetypeConfig";
import { CONFIGURE_REQUIRED_COPY, HARD_GATE_HINT_WIZARD, HARD_GATE_HINT_WIZARD_COLLAPSED } from "../src/lib/pickerCopy";
import { ARCHETYPES } from "./fixtures.mjs";

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

const coderTrigger = (wizard) => wizard.locator('[id="config:project_board.coder"]');
// The contract note, computed by the helper the card renders with (no wording drift).
const PM = ARCHETYPES.find((a) => a.id === "project-manager");
const PM_CONTRACT_NOTICE = requiresToolsNotice(PM.label, PM.requires_tools);

test("picking Project Manager shows its capability contract and the required-answer hint", async ({ page }) => {
  const wizard = await openWizard(page);
  await pickProjectManager(wizard);
  await expect(wizard.getByRole("note").filter({ hasText: PM_CONTRACT_NOTICE })).toBeVisible();
  await expect(wizard.getByRole("button", { name: /Configure Project Manager/ })).toContainText(CONFIGURE_REQUIRED_COPY);
  await expect(wizard.getByText(HARD_GATE_HINT_WIZARD, { exact: true })).toBeVisible();
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
  await expect(wizard.getByText(HARD_GATE_HINT_WIZARD_COLLAPSED, { exact: true })).toBeVisible();
});

test("both required answers enable Finish; config_inputs ride the bundle install even after collapsing Configure (#2979)", async ({ page }) => {
  const wizard = await openWizard(page);
  // Capture the install body and answer it HERE: the mock's real handler mutates
  // module-global plugin state (INSTALLED_PLUGINS / RUNTIME_STATUS.plugins / the settings
  // schema — not header-scoped like fleet/mcp), which other specs read under fullyParallel.
  let installed = null;
  let setup = null;
  // #2989: Finish also records the archetype's capability contract on the host —
  // the wire body of POST /api/config/setup must carry requires_tools.
  await page.route("**/api/config/setup", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    setup = route.request().postDataJSON();
    return route.continue();
  });
  await page.route("**/api/plugins/install", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    installed = route.request().postDataJSON();
    return route.fulfill({
      json: {
        installed: { id: "project-manager-archetype", name: "Project Manager", version: "0.1.0", description: "", resolved_sha: "abc", source_url: installed.url, requires_pip: [], capabilities: {}, contributes: { views: [], secrets: [] } },
        enabled: [],
        reloaded: true,
        restart_recommended: false,
        enable_error: null,
      },
    });
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
  await expect(wizard.getByText(HARD_GATE_HINT_WIZARD, { exact: true })).toHaveCount(0);

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
  // The host records the contract the persona commits to (#2989) — the post-boot
  // capability banner has something to check against on the wizard path too.
  await expect.poll(() => setup).not.toBeNull();
  expect(setup?.requires_tools).toEqual(["github_create_issue"]);
});
