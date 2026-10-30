import { expect, type Page } from "@playwright/test";
import type { Flow } from "./runner";

// Copied from the Career Coach entry in the curated archetype catalog.
export const careerCoachArchetype = {
  "id": "career-coach",
  "label": "Career Coach",
  "icon": "Compass",
  "bundle": "https://github.com/protoLabsAI/careercoach-archetype",
  "blurb": "A job-hunt copilot — paste your resume, get a profile and fresh roles in minutes; triage an inbox, track a pipeline, and get an apply kit (tailored resume, cover letter, every answer) you fill in yourself. Never applies on its own.",
  "soul_preset": "career-coach",
  "requires_tools": [
    "careercoach_find_roles",
    "careercoach_save_apply_kit"
  ]
};

// Kept as a factory so retries and contract tests own their creation receipt.
export function careerCoachFlow(live: boolean): Flow {
  let name: string;
  let agentId: string;
  return {
    name: "new-agent-careercoach", version: 1,
    steps: [
      { name: "Open console", async run(page: Page) {
        name = live ? `flow-cc-${Date.now()}` : "flow-cc-mock";
        agentId = "";
        if (!live) {
          const scope = `careercoach-${process.pid}-${Date.now()}`;
          await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope });
          expect((await page.request.post("/api/__test__/fleet/reset", { headers: { "x-e2e-fleet": scope } })).ok()).toBe(true);
          await page.route("**/api/archetypes*", async (route) => {
            const response = await route.fetch();
            const body = await response.json();
            await route.fulfill({ response, json: {
              ...body, archetypes: [...body.archetypes, careerCoachArchetype],
            } });
          });
        }
        await page.goto("/app/");
        await expect(page.getByTestId("header-menu")).toBeVisible();
      } },
      { name: "Open Fleet", async run(page: Page) {
        await page.getByTestId("header-menu").click();
        await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
        await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Fleet", exact: true }).click();
        await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
      } },
      { name: "New agent", async run(page: Page) {
        await page.getByRole("button", { name: "New agent", exact: true }).click();
        await expect(page.getByRole("heading", { name: "New agent" })).toBeVisible();
      } },
      { name: "Pick Career Coach", async run(page: Page) {
        await page.locator(".pl-radiocard", { hasText: "Career Coach" }).click();
      } },
      { name: "Next", async run(page: Page) {
        await page.getByRole("button", { name: /^Next/ }).click();
        await expect(page.locator(".archetype-setup-dialog")).toBeVisible();
        await expect(page.getByLabel("Agent name")).toHaveValue("career-coach");
      } },
      { name: "Set name", async run(page: Page) {
        await page.getByLabel("Agent name").fill(name);
      } },
      { name: "Create and enter agent", async run(page: Page) {
        // Read the creation receipt before navigation can discard the response body.
        let receipt: { ok: boolean; agent: { id: string; name: string } };
        await page.route("**/api/fleet", async (route) => {
          if (route.request().method() !== "POST") return route.fallback();
          const response = await route.fetch({ timeout: 45_000 });
          receipt = await response.json();
          await route.fulfill({ response });
        });
        await page.locator(".archetype-setup-dialog").getByRole("button", { name: /^Create/ }).click();
        await expect.poll(() => receipt).toBeTruthy();
        expect(receipt!.ok).toBe(true);
        expect(receipt!.agent.name).toBe(name);
        expect(typeof receipt!.agent.id).toBe("string");
        expect(receipt!.agent.id).toBeTruthy();
        agentId = receipt!.agent.id;
        await expect(page).toHaveURL((url) => url.pathname === `/app/agent/${encodeURIComponent(agentId)}/`);
        await expect(page.getByTestId("fleet-switcher")).toContainText(name);
      } },
      ...(live ? [{ name: "Delete created agent", async run(page: Page) {
        expect(agentId).toBeTruthy();
        // Hub-scoped API, using only the immutable ID returned by this run's POST.
        const result = await page.evaluate(async (id) => {
          const response = await fetch(`/api/fleet/${encodeURIComponent(id)}`, {
            method: "DELETE",
            headers: { Authorization: `Bearer ${localStorage.getItem("protoagent.authToken")}` },
          });
          return { status: response.status, ok: response.ok, body: await response.json() };
        }, agentId);
        expect(result.ok, `Delete returned HTTP ${result.status}`).toBe(true);
        expect(result.body.ok).toBe(true);
        await page.goto("/app/");
      } }] : []),
    ],
  };
}
