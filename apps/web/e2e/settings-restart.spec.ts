import { expect, test } from "@playwright/test";

test("saved restart changes wait for idle and clear only after reconnect", async ({ page }) => {
  let idle = false;
  let boot = "old";
  let restarts = 0;
  await page.route("**/api/restart/readiness", (route) => route.fulfill({ json: {
    idle, busy: idle ? [] : ["a chat turn is running", "2 background jobs"],
    supervised: true, how_to: "", boot_id: boot,
  } }));
  await page.route("**/api/restart", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({ when_idle: true });
    expect(route.request().postDataJSON().reason).toContain("settings:");
    restarts++;
    await route.fulfill({ status: 202, json: { ok: true } });
  });
  await page.route("**/api/settings", (route) => route.fulfill({ json: {
    ok: true, messages: ["Saved"], restart_required: ["runtime.autostart_on_boot"],
  } }));
  await page.goto("/app/");
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Behavior", exact: true }).click();
  const triggers = page.locator(".pl-accordion__trigger");
  for (let i = 0; i < await triggers.count(); i++) {
    if (await triggers.nth(i).getAttribute("aria-expanded") !== "true") await triggers.nth(i).click();
  }
  await page.locator('[data-key="runtime.autostart_on_boot"] .pl-switch').click();
  await page.getByRole("button", { name: "Save & apply", exact: true }).click();
  const banner = page.locator(".settings-restart-banner");
  await expect(banner).toContainText("2 background jobs");
  await banner.getByRole("button", { name: "Restart when idle" }).click();
  await page.getByRole("dialog", { name: "Restart this agent?" }).getByRole("button", { name: "Restart when idle" }).click();
  await expect(banner).toContainText("Waiting for idle");
  expect(restarts).toBe(0);
  idle = true;
  await expect.poll(() => restarts).toBe(1);
  await expect(banner).toBeVisible();
  boot = "new";
  await expect(banner).toHaveCount(0);
});
