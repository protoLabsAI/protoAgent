import { expect, test } from "@playwright/test";

// Auth UX (#873): a token-gated deployment answers 401 until the operator
// supplies the bearer. The console must surface a token prompt (not just
// per-panel 401 cards), persist the token, and recover in place. The mock
// server isn't token-gated, so the gate is simulated by intercepting /api/*
// and rejecting requests that don't carry the expected Authorization header.

const TOKEN = "e2e-operator-token";

test("a 401 opens the token prompt; saving recovers in place and persists", async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const auth = route.request().headers()["authorization"] || "";
    if (auth === `Bearer ${TOKEN}`) return route.fallback(); // through to the mock
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Unauthorized" }),
    });
  });

  await page.goto("/app/", { waitUntil: "load" });

  // The boot probe 401s → the prompt opens (and the BootGate yields to it).
  const dialog = page.getByRole("dialog", { name: "Authentication required" });
  await expect(dialog).toBeVisible({ timeout: 10_000 });

  await dialog.getByLabel("Operator token").fill(TOKEN);
  await dialog.getByRole("button", { name: "Connect", exact: true }).click();

  // Recovery in place: the prompt closes and the app reaches the chat surface
  // without a reload (queries refetch with the bearer attached).
  await expect(dialog).not.toBeVisible();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible({ timeout: 15_000 });

  // The token persisted where authToken() reads it.
  const stored = await page.evaluate(() => window.localStorage.getItem("protoagent.authToken"));
  expect(stored).toBe(TOKEN);
});

test("'Not now' dismisses; the next 401 re-prompts", async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Unauthorized" }),
    });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const dialog = page.getByRole("dialog", { name: "Authentication required" });
  await expect(dialog).toBeVisible({ timeout: 10_000 });
  await dialog.getByRole("button", { name: "Not now", exact: true }).click();
  await expect(dialog).not.toBeVisible();

  // Dismissing doesn't loop the prompt: the 401-aware boot probe stops retrying,
  // and the BootGate reappears in its failed state. Its Retry fires a fresh
  // request → 401 → the prompt returns.
  await page.locator(".pl-bootgate").getByRole("button", { name: "Retry", exact: true }).click();
  await expect(dialog).toBeVisible({ timeout: 10_000 });
});
