import { devices, expect, test } from "@playwright/test";

for (const [name, profile] of [
  ["desktop", { viewport: { width: 1440, height: 900 } }],
  ["iPad Mini portrait", devices["iPad Mini"]],
  ["narrow phone", { ...devices["iPhone SE"], viewport: { width: 320, height: 568 } }],
] as const) {
  test.describe(name, () => {
    const { defaultBrowserType: _browser, ...options } = { defaultBrowserType: "chromium", ...profile };
    test.use(options);
    test("repeated completions stay between rails and remain dismissible", async ({ page }, testInfo) => {
      await page.addInitScript(() => {
        // Exercise the real event bus and App subscriptions without timer/reconnect races.
        class Events extends EventTarget {
          onmessage: ((event: MessageEvent) => void) | null = null;
          onopen: (() => void) | null = null;
          close() {}
          constructor(url: string) {
            super();
            if (url.includes("/api/events")) (window as any).completeGoal = (condition: string) => this.onmessage?.(
              new MessageEvent("message", { data: JSON.stringify({ topic: "goal.achieved", data: { condition } }) }),
            );
            setTimeout(() => this.onopen?.(), 0);
          }
        }
        window.EventSource = Events as any;
      });
      await page.goto("/app/");
      await expect.poll(() => page.evaluate(() => typeof (window as any).completeGoal)).toBe("function");
      await page.evaluate(() => {
        for (const condition of ["Repeated goal", "Repeated goal", "Distinct goal A: complete the full regression suite and publish the verification results", "Distinct goal B"])
          (window as any).completeGoal(condition);
      });
      const stack = page.getByRole("region", { name: "Notifications" });
      const toasts = stack.getByRole("status");
      await expect(toasts).toHaveCount(4);
      await expect(stack).toContainText("Distinct goal A");
      await expect(stack).toContainText("Distinct goal B");
      const rails = page.locator(".app-shell-main .pl-rail:not(.pl-rail--bottom)");
      const bounds = await toasts.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().toJSON()));
      const railBounds = await rails.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().toJSON()).filter((r) => r.width && r.height));
      for (const toast of bounds) {
        expect(toast.x).toBeGreaterThanOrEqual(0);
        expect(toast.right).toBeLessThanOrEqual(page.viewportSize()!.width);
        expect(toast.y).toBeGreaterThanOrEqual(0);
        expect(toast.bottom).toBeLessThanOrEqual(page.viewportSize()!.height);
        for (const rail of railBounds)
          expect(toast.right <= rail.x || toast.x >= rail.right).toBe(true);
      }
      if (name !== "narrow phone") {
        const work = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
        const workBounds = await work.boundingBox();
        expect(railBounds).toHaveLength(2);
        await work.click({ trial: true }); // hit testing must succeed while all four alerts are visible
        await testInfo.attach("geometry", { body: JSON.stringify({ viewport: page.viewportSize(), toasts: bounds, rails: railBounds, work: workBounds }, null, 2), contentType: "application/json" });
      }
      await toasts.filter({ hasText: "Distinct goal A" }).getByRole("button", { name: "Dismiss", exact: true }).click();
      await expect(toasts).toHaveCount(3);
      await expect(stack).toContainText("Distinct goal B");
    });
  });
}
