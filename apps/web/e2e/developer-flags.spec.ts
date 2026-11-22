import { expect, test } from "@playwright/test";

// Developer flags panel (ADR 0068). The mock serves channel "dev", so the Developer section
// appears under Settings ▸ This console. A toggle is stored on the AGENT, so it reaches the
// server and is still there after a reload with this browser's storage wiped; Reset clears it.

test("Developer panel: a toggle is stored on the agent, survives a reload, and Reset clears it", async ({ page }) => {
  const open = async () => {
    await page.goto("/app/", { waitUntil: "load" });
    await page.getByTestId("settings-widget").click();
    await expect(page.locator(".settings-overlay")).toBeVisible();
    // Off prod (mock channel = dev) the Developer section is present.
    await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Developer", exact: true }).click();
    await expect(page.getByTestId("developer-panel")).toBeVisible();
  };
  await open();
  const panel = page.getByTestId("developer-panel");
  await expect(panel.getByText("chat.new_dashboard")).toBeVisible();
  await expect(panel.getByText("chat.experimental_widget")).toBeVisible();

  const row = panel.locator('[data-key="flag.chat.new_dashboard"]');
  const toggle = row.getByRole("checkbox");
  try {
    await expect(row.getByText("overridden")).toHaveCount(0);
    await expect(toggle).toBeChecked();

    // Toggle it off: the change goes to the agent, not just this browser.
    const saved = page.waitForResponse((r) => r.url().includes("/api/flags/chat.new_dashboard") && r.request().method() === "PUT");
    await row.locator(".pl-switch").click();
    expect((await saved).request().postDataJSON()).toEqual({ enabled: false });
    await expect(row.getByText("overridden")).toBeVisible();
    await expect(toggle).not.toBeChecked();
    await expect(page.getByText("Saved on this device only")).toHaveCount(0);

    // Nothing in this browser is carrying it: wipe local state and reload.
    await page.evaluate(() => localStorage.clear());
    await open();
    await expect(row.getByText("overridden")).toBeVisible();
    await expect(toggle).not.toBeChecked();

    // Reset → the agent's override clears and the channel default is back.
    const cleared = page.waitForResponse((r) => r.url().includes("/api/flags/chat.new_dashboard") && r.request().method() === "DELETE");
    await row.getByRole("button", { name: "Reset" }).click();
    await cleared;
    await expect(row.getByText("overridden")).toHaveCount(0);
    await expect(toggle).toBeChecked();
  } finally {
    // The mock agent is shared by every spec in the run; never leave an override behind.
    await page.request.delete("/api/flags");
  }
});
