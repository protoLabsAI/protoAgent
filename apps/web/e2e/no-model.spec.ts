import { expect, test } from "@playwright/test";

import { STARTER_PROMPTS } from "../src/chat/starterPrompts";

test("explore chat blocks sends, links model settings, and recovers on a runtime poll", async ({ page }) => {
  let configured = false;
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const runtime = await response.json();
    await route.fulfill({ json: { ...runtime, model_configured: configured, graph_loaded: configured } });
  });
  await page.goto("/app/");
  const banner = page.getByText("No model connected. The agent can't reply yet.", { exact: false });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(banner).toBeVisible();
  await expect(composer).toBeDisabled();
  const starter = STARTER_PROMPTS[0];
  await page.getByRole("button", { name: starter.note ? `${starter.label} ${starter.note}` : starter.label, exact: true }).click();
  await expect(page.locator(".pl-message--user")).toHaveCount(0);
  await page.getByRole("button", { name: "Connect a model", exact: true }).click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Model", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Escape");
  configured = true;
  await expect(banner).toHaveCount(0, { timeout: 20_000 });
  await expect(composer).toBeEnabled();
});
