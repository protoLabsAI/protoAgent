import { expect, test, type Page } from "@playwright/test";

const providers = ["anthropic-oauth", "openai-codex"];
const reset = new Date(Date.now() + 3_600_000).toISOString();
const windows = [
  { id: "session", kind: "session", used_pct: 25, status: "ok", resets_at: reset },
  { id: "weekly", kind: "weekly", used_pct: 80, status: "ok", resets_at: reset, scope: { model: "Fable" } },
  { id: "monthly", kind: "monthly", used_pct: 95, status: "blocked", resets_at: reset },
];

async function setup(page: Page) {
  await page.route("**/api/config/providers", (route) => route.fulfill({ json: {
    providers: providers.map((id) => ({ id, type: id, display: id, has_key: false, in_use_by: [] })),
  } }));
  await page.route("**/api/config/oauth-status", (route) => route.fulfill({ json: {
    providers: providers.map((provider) => ({ provider, signed_in: true, detail: "Test account", hint: "" })),
  } }));
  // Exercise the real shared EventSource dispatcher with controllable host frames.
  await page.addInitScript(() => {
    const sources: { onmessage?: (event: { data: string }) => void }[] = [];
    class FakeEventSource {
      onmessage?: (event: { data: string }) => void;
      onopen?: () => void;
      constructor() { sources.push(this); setTimeout(() => this.onopen?.(), 0); }
      close() {}
    }
    Object.assign(window, {
      EventSource: FakeEventSource,
      emitUsage: (provider_id: string) => sources.forEach((s) => s.onmessage?.({
        data: JSON.stringify({ topic: "provider.usage", data: { provider_id } }),
      })),
    });
  });
}

async function openProviders(page: Page) {
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  await expect(page.getByTestId("providers-panel")).toBeVisible();
}

for (const theme of ["light", "dark"] as const) {
  test(`usage states and live updates in ${theme}`, async ({ page }) => {
    await setup(page);
    await page.emulateMedia({ colorScheme: theme });
    await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode: theme, overrides: {} } } }));
    let unknown = false;
    await page.route("**/api/usage?*", (route) => {
      const provider_id = new URL(route.request().url()).searchParams.get("account");
      return route.fulfill({ json: { accounts: [{ provider_id, status: unknown ? "unknown" : "ok", windows }] } });
    });
    await page.goto("/app/");
    await openProviders(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const meters = page.getByTestId("usage-meter");
    await expect(meters).toHaveCount(2);
    for (const meter of await meters.all()) {
      await expect(meter.getByRole("progressbar")).toHaveCount(3);
      await expect(meter.getByRole("group", { name: "Weekly · Fable" })).toHaveAttribute("data-state", "warning");
      await expect(meter.getByRole("group", { name: "Session", exact: true })).toHaveAttribute("data-state", "ok");
      await expect(meter.getByRole("group", { name: "Monthly" })).toHaveAttribute("data-state", "blocked");
      await expect(meter).toContainText("25%");
      await expect(meter).toContainText("80%");
      await expect(meter).toContainText("Blocked until");
      await expect(meter).toContainText("Resets in");
    }
    unknown = true;
    await page.evaluate((ids) => ids.forEach((id) => (window as unknown as { emitUsage: (id: string) => void }).emitUsage(id)), providers);
    for (const meter of await meters.all()) {
      await expect(meter).toHaveText("Usage unavailable");
      await expect(meter.getByRole("alert")).toHaveCount(0);
      await expect(meter.getByRole("progressbar")).toHaveCount(0);
    }
  });
}

test("usage never fetches while Settings is closed, including after unmount", async ({ page }) => {
  await setup(page);
  let requests = 0;
  await page.route("**/api/usage?*", (route) => {
    requests++;
    return route.fulfill({ json: { accounts: [] } });
  });
  await page.clock.install();
  await page.goto("/app/");
  await expect(page.getByTestId("settings-widget")).toBeVisible();
  await page.clock.fastForward(120_000);
  expect(requests).toBe(0);
  await openProviders(page);
  await expect.poll(() => requests).toBe(2);
  await page.keyboard.press("Escape");
  await expect(page.locator(".settings-overlay")).toHaveCount(0);
  await page.evaluate(() => (window as unknown as { emitUsage: (id: string) => void }).emitUsage("openai-codex"));
  await page.clock.fastForward(120_000);
  expect(requests).toBe(2);
});

test("stale and failed reads are unavailable", async ({ page }) => {
  await setup(page);
  await page.route("**/api/usage?*", (route) => new URL(route.request().url()).searchParams.get("account") === "openai-codex"
    ? route.fulfill({ status: 503, body: "unavailable" })
    : route.fulfill({ json: { accounts: [{ provider_id: "anthropic-oauth", status: "ok", stale: true, windows }] } }));
  await page.goto("/app/");
  await openProviders(page);
  await expect(page.getByTestId("usage-meter")).toHaveText(["Usage unavailable", "Usage unavailable"]);
});
