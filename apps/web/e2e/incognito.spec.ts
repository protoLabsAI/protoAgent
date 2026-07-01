import { expect, test } from "@playwright/test";

// Incognito threads (ADR 0069 D3b): while a tab's toggle is ON, EVERY message it
// sends carries metadata.incognito — the backend flag is per-message, so a single
// missed send would leak that turn into memory. Asserted at the WIRE level by
// capturing the /a2a request bodies.

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

function captureA2ABodies(page: import("@playwright/test").Page): { metadata?: Record<string, unknown> }[] {
  const messages: { metadata?: Record<string, unknown> }[] = [];
  page.on("request", (req) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    try {
      const body = JSON.parse(req.postData() || "{}");
      if (body?.method === "SendStreamingMessage") messages.push(body.params?.message ?? {});
    } catch {
      // non-JSON /a2a traffic — not a chat turn
    }
  });
  return messages;
}

test("/incognito toggles the tab: chip shows, every send carries metadata.incognito, off stops stamping", async ({ page }) => {
  const sent = captureA2ABodies(page);
  const composer = page.getByPlaceholder(/Message protoAgent/i);

  // Turn incognito ON via the slash command (bare /incognito toggles). The system
  // note confirms the local action took effect.
  await composer.fill("/incognito");
  await composer.press("Enter"); // picks the highlighted client command → runs it
  await expect(page.locator(".chat-note", { hasText: "Incognito ON" })).toBeVisible();
  // The composer chip is the persistent visual indicator while ON.
  await expect(page.locator(".composer-incognito-toggle")).toBeVisible();

  // BOTH messages sent while ON carry the flag (per-message contract).
  await composer.fill("first private message");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user", { hasText: "first private message" })).toBeVisible();
  await expect(page.locator(".pl-message--assistant").first()).toBeVisible();
  await composer.fill("second private message");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user", { hasText: "second private message" })).toBeVisible();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[0].metadata?.incognito).toBe(true);
  expect(sent[1].metadata?.incognito).toBe(true);

  // Clicking the chip turns incognito OFF; the next send carries NO incognito flag.
  await page.locator(".composer-incognito-toggle").click();
  await expect(page.locator(".composer-incognito-toggle")).toHaveCount(0);
  await composer.fill("back to normal");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user", { hasText: "back to normal" })).toBeVisible();
  await expect.poll(() => sent.length).toBe(3);
  expect(sent[2].metadata?.incognito).toBeUndefined();
});

test("the chat-tab context menu offers New incognito chat + a per-tab toggle", async ({ page }) => {
  const tabs = page.locator(".pl-tabbar__tab");
  const menu = page.locator(".pl-menu");

  // Right-click the current tab → toggle incognito on from the menu.
  await tabs.first().click({ button: "right" });
  await expect(menu).toBeVisible();
  await menu.getByText("Turn incognito on", { exact: true }).click();
  await expect(page.locator(".composer-incognito-toggle")).toBeVisible();
  // The tab itself shows the incognito glyph while ON.
  await expect(page.locator(".session-incognito-icon")).toBeVisible();

  // Re-open the menu: the entry now offers to turn it off.
  await tabs.first().click({ button: "right" });
  await menu.getByText("Turn incognito off", { exact: true }).click();
  await expect(page.locator(".composer-incognito-toggle")).toHaveCount(0);

  // "New incognito chat" opens a NEW tab already in incognito.
  await tabs.first().click({ button: "right" });
  await menu.getByText("New incognito chat", { exact: true }).click();
  await expect(tabs).toHaveCount(2);
  await expect(page.locator(".composer-incognito-toggle")).toBeVisible();
  await expect(page.locator(".session-incognito-icon")).toBeVisible();
});
