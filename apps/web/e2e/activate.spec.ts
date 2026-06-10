import { expect, test } from "@playwright/test";

// Slug navigation → activate (#806): a window opening /app/agent/<slug>/ ensures its
// agent is running (cold resume from checkpoint) + touches it for keep-N-warm. The
// fixture's "roxy" is STOPPED, so this is the exact navigate-to-a-cold-agent path.

test("opening a slug page activates (resumes) the agent", async ({ page }) => {
  const activated = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().includes("/api/fleet/roxy/activate"),
  );
  await page.goto("/app/agent/roxy/", { waitUntil: "load" });
  await activated; // boot fired the resume call AND the mock processed it

  // And the mock fleet now reports it running.
  await expect
    .poll(async () => {
      const fleet = await page.evaluate(() => fetch("/api/fleet").then((r) => r.json()));
      return fleet.agents.find((a: { id: string }) => a.id === "roxy")?.running;
    })
    .toBe(true);
});

test("the host window never calls activate", async ({ page }) => {
  const activateCalls: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/activate")) activateCalls.push(r.url());
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // app booted
  expect(activateCalls).toEqual([]);
});
