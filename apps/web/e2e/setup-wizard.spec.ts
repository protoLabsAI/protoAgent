import { expect, test } from "@playwright/test";

import { requiresToolsNotice } from "../src/lib/archetypeConfig";
import { HARD_GATE_HINT_WIZARD, SETUP_REQUIRED_HELP } from "../src/lib/pickerCopy";
import { ARCHETYPES } from "./fixtures.mjs";
import { routeSnapshot } from "./routeSnapshot";

// Model-first setup with identity and bundle questions in one agent step.
//
// The mock reports setup_complete:true for every other spec; these flip it per-test so
// the wizard mounts over the shell. Finishing never flips it back (the override is
// sticky), which keeps the wizard open for the post-Finish payload assertions.
//
// The status is served from a one-time snapshot, never a per-request `route.fetch()` proxy:
// Finish's blanket refetch (setup/finish.ts) fires just as the last test ends, and teardown
// disposes a proxied body mid-read ("Response has been disposed") — see routeSnapshot.ts.

async function openWizard(page) {
  await routeSnapshot(page, "/api/runtime/status", (json) => {
    json.setup_complete = false;
  });
  await page.goto("/app/", { waitUntil: "load" });
  const wizard = page.getByRole("dialog", { name: "Setup" });
  await expect(wizard).toBeVisible();
  // welcome → brain → agent
  await wizard.getByRole("button", { name: "Next" }).click();
  await wizard.getByRole("button", { name: "Test connection", exact: true }).click();
  await expect(wizard.getByRole("button", { name: "Next" })).toBeEnabled();
  await wizard.getByRole("button", { name: "Next" }).click();
  await expect(wizard.locator(".archetype-picker")).toBeVisible();
  await expect(wizard.getByLabel("Agent name")).toHaveValue("mock-agent");
  await expect(wizard.locator(".setup-progress span")).toHaveCount(4);
  await expect(wizard.locator(".pl-radiocard", { hasText: "Project Manager" })).toHaveCount(0);
  await wizard.getByRole("button", { name: "More archetypes…" }).click();
  return wizard;
}

// Pick the advanced Project Manager card to reveal its questions.
async function pickProjectManager(wizard) {
  await wizard.getByRole("button", { name: /^Advanced \(1\)/ }).click();
  await wizard.locator(".pl-radiocard", { hasText: "Project Manager" }).click();
  await expect(wizard.getByLabel("Repository path")).toBeVisible();
}

// Agent → finish after answering the required bundle questions.
async function goToFinish(wizard) {
  await wizard.getByRole("button", { name: "Next" }).click();
  await expect(wizard.getByRole("button", { name: "Finish" })).toBeVisible();
}

const coderTrigger = (wizard) => wizard.locator('[id="config:project_board.coder"]');
// The contract note, computed by the helper the card renders with (no wording drift).
const PM = ARCHETYPES.find((a) => a.id === "project-manager");
const PM_CONTRACT_NOTICE = requiresToolsNotice(PM.label, PM.requires_tools);

test("the agent step shows the capability contract, name, help lines and required note", async ({ page }) => {
  const wizard = await openWizard(page);
  await wizard.getByRole("button", { name: /^Advanced \(1\)/ }).click();
  await wizard.locator(".pl-radiocard", { hasText: "Project Manager" }).click();
  await expect(wizard.getByRole("note").filter({ hasText: PM_CONTRACT_NOTICE })).toBeVisible();

  await expect(wizard.getByRole("heading", { name: "Set up Project Manager" })).toBeVisible();
  await expect(wizard.getByLabel("Agent name")).toHaveValue("project-manager");
  await expect(wizard.getByText(SETUP_REQUIRED_HELP, { exact: true })).toBeVisible();
  await expect(wizard.getByText("The local checkout this board manages — registered as a project.")).toBeVisible();
  await expect(wizard.getByRole("button", { name: /Browse/ })).toBeVisible();
  await expect(wizard.getByText(HARD_GATE_HINT_WIZARD, { exact: true })).toBeVisible();
});

test("the agent step's Next stays disabled while a required bundle answer is blank (#2977)", async ({ page }) => {
  const wizard = await openWizard(page);
  await pickProjectManager(wizard);
  await wizard.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await expect(wizard.getByRole("button", { name: "Next" })).toBeDisabled();
});

test("Back to the model and forward keeps every agent answer", async ({ page }) => {
  const wizard = await openWizard(page);
  await pickProjectManager(wizard);
  await wizard.getByLabel("Agent name").fill("hq");
  await wizard.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await wizard.getByRole("button", { name: "Back" }).click();
  await wizard.getByRole("button", { name: "Next" }).click();
  await expect(wizard.locator(".pl-radiocard--selected", { hasText: "Project Manager" })).toBeVisible();
  await expect(wizard.getByLabel("Agent name")).toHaveValue("hq");
  await expect(wizard.getByLabel("Repository path")).toHaveValue("/Users/me/dev/repo");
});

test("both required answers unlock Next; config_inputs ride the bundle install on Finish (#2979)", async ({ page }) => {
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

  await goToFinish(wizard);
  const finish = wizard.getByRole("button", { name: "Finish" });
  await expect(finish).toBeEnabled();
  await finish.click();

  await expect.poll(() => installed).not.toBeNull();
  expect(installed?.url).toBe("https://github.com/protoLabsAI/project-manager-archetype");
  expect(installed?.config_inputs).toEqual({ "project_board.repo": "/Users/me/dev/repo", "project_board.coder": "coder" });
  // The host records the contract the persona commits to (#2989).
  await expect.poll(() => setup).not.toBeNull();
  expect(setup?.requires_tools).toEqual(["github_create_issue"]);
});

test("the local model step stays short and shows only what's usable", async ({ page }) => {
  await page.route("**/api/config/local-models", (route) =>
    route.fulfill({
      json: {
        endpoints: [
          { id: "ollama", label: "Ollama", base_url: "http://127.0.0.1:11434/v1", found: true, models: ["qwen3:14b"], error: "" },
          {
            id: "lmstudio",
            label: "LM Studio",
            base_url: "http://127.0.0.1:1234/v1",
            found: false,
            models: [],
            error: "Nothing is answering at http://127.0.0.1:1234/v1/models.",
          },
        ],
      },
    }),
  );
  await page.route("**/api/config/test-model", (route) => route.fulfill({ json: { ok: true, error: "" } }));
  await routeSnapshot(page, "/api/runtime/status", (json) => {
    json.setup_complete = false;
  });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/app/", { waitUntil: "load" });

  const wizard = page.getByRole("dialog", { name: "Setup" });
  await wizard.getByRole("button", { name: "Next" }).click();
  await wizard.locator(".pl-radiocard", { hasText: "Local model" }).click();

  // The test runs by itself and unlocks Next: no button to hunt for.
  await expect(wizard.getByText("Connection OK")).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Next" })).toBeEnabled();
  await expect(wizard.getByRole("button", { name: "Test connection", exact: true })).toHaveCount(0);
  await expect(wizard.getByRole("button", { name: "Load models" })).toHaveCount(0);

  await expect(wizard.locator(".setup-local-row")).toHaveCount(1);
  await expect(wizard.locator(".setup-local-row")).toContainText("Ollama");
  await expect(wizard.getByText("LM Studio", { exact: true })).toHaveCount(0);
  await expect(wizard.getByText("Nothing is answering")).toHaveCount(0);
  await expect(wizard.getByPlaceholder("e.g. http://127.0.0.1:11434/v1")).toBeHidden();

  // All four cards share a row, and the whole step fits with its actions on screen.
  const tops = await wizard.locator(".pl-radiocard").evaluateAll((cards) => cards.map((c) => c.getBoundingClientRect().top));
  expect(new Set(tops).size).toBe(1);
  const next = (await wizard.getByRole("button", { name: "Next" }).boundingBox())!;
  expect(next.y + next.height).toBeLessThanOrEqual(720);
});

test("a local model that fails its test offers Try again without resending anything by itself", async ({ page }) => {
  await page.route("**/api/config/local-models", (route) =>
    route.fulfill({
      json: {
        endpoints: [
          { id: "ollama", label: "Ollama", base_url: "http://127.0.0.1:11434/v1", found: true, models: ["qwen3:14b"], error: "" },
        ],
      },
    }),
  );
  let calls = 0;
  await page.route("**/api/config/test-model", (route) => {
    calls += 1;
    return route.fulfill({ json: calls === 1 ? { ok: false, error: "The endpoint didn't answer in time. (timeout)" } : { ok: true, error: "" } });
  });
  await routeSnapshot(page, "/api/runtime/status", (json) => {
    json.setup_complete = false;
  });
  await page.goto("/app/", { waitUntil: "load" });

  const wizard = page.getByRole("dialog", { name: "Setup" });
  await wizard.getByRole("button", { name: "Next" }).click();
  await wizard.locator(".pl-radiocard", { hasText: "Local model" }).click();

  await expect(wizard.getByText("Connection failed")).toBeVisible();
  await expect(wizard.getByText("fix it to continue")).toHaveCount(0);
  expect(calls).toBe(1);
  await wizard.getByRole("button", { name: "Try again" }).click();
  await expect(wizard.getByText("Connection OK")).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Next" })).toBeEnabled();
});
