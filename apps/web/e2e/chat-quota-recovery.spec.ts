import { expect, test } from "@playwright/test";

test("changing model waits for an older REST turn to stop and keeps the same chat", async ({ page }) => {
  let active = true;
  let stopped = false;
  let sessionId = "";
  await page.route("**/api/chat/sessions/*/local-turns", async (route) => {
    sessionId = new URL(route.request().url()).pathname.split("/").at(-2)!;
    await route.fulfill({ json: { turns: active ? [{ id: "blocked", origin: "api-chat", started_at: new Date().toISOString(), cancel_requested: false }] : [] } });
  });
  await page.route("**/api/chat/sessions/*/local-turns/blocked/cancel", async (route) => {
    expect(route.request().method()).toBe("POST");
    stopped = true;
    active = false;
    await route.fulfill({ json: { stopped: true } });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByRole("button", { name: "Stop request" })).toBeVisible();
  const originalSession = sessionId;
  await page.getByRole("button", { name: "Model for this chat" }).click();
  await page.getByRole("menuitem", { name: "protolabs/fast", exact: true }).first().click();
  await expect(page.getByRole("button", { name: "Model for this chat" })).toContainText("protolabs/fast");
  expect(stopped).toBe(true);
  expect(sessionId).toBe(originalSession);
  await expect(page.getByRole("button", { name: "Stop request" })).not.toBeVisible();
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("continue after quota");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").filter({ hasText: "continue after quota" })).toBeVisible();
});
