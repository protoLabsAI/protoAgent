import { expect, test } from "@playwright/test";

// The chat surface is a SLOT (ADR 0045): a plugin view declaring `slot: "chat"`
// replaces the built-in chat panel — rendered under the core "chat" rail id, kept
// mounted for the app's lifetime (#613 streaming continuity), and given no extra
// rail icon. These specs inject a claimant into the runtime fixture per-test;
// every other spec keeps the default fixture, proving the built-in default.

async function withChatSlotPlugin(page: import("@playwright/test").Page) {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.plugins.push({
      id: "chatty",
      name: "Chatty",
      version: "0.1.0",
      enabled: true,
      loaded: true,
      tools: [],
      skills: 0,
      views: [{ id: "panel", label: "Chatty", icon: "MessageSquare", path: "/plugins/chatty/panel", slot: "chat" }],
    });
    await route.fulfill({ json });
  });
}

test("a slot:chat plugin view replaces the built-in chat panel", async ({ page }) => {
  await withChatSlotPlugin(page);
  await page.goto("/app/", { waitUntil: "load" });

  // The core Chat rail item is still there — the slot keeps the id, the plugin
  // provides the panel.
  const chatBtn = page.locator(".pl-rail").getByRole("button", { name: "Chat", exact: true });
  await expect(chatBtn).toBeVisible();

  // The chat area hosts the plugin's iframe instead of the built-in surface.
  const frame = page.locator(".chat-slot .plugin-view-frame");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /\/plugins\/chatty\/panel/);
  await expect(page.locator(".chat-tabbar")).toHaveCount(0); // built-in surface not mounted

  // No SECOND rail icon for the claimant — it lives under "chat", not plugin:chatty:panel.
  await expect(
    page.locator(".pl-rail").getByRole("button", { name: "Chatty", exact: true }),
  ).toHaveCount(0);
});

test("a slot claimant stays mounted across surface switches (#613 contract)", async ({ page }) => {
  await withChatSlotPlugin(page);
  await page.goto("/app/", { waitUntil: "load" });

  const slotFrame = page.locator(".chat-slot .plugin-view-frame");
  await expect(slotFrame).toBeVisible();

  // Switch away — a NORMAL plugin view would unmount (plugin-views.spec asserts
  // count 0); the slot claimant must stay in the DOM, merely hidden.
  await page.locator(".pl-rail").getByRole("button", { name: "Activity", exact: true }).click();
  await expect(slotFrame).toHaveCount(1);
  await expect(slotFrame).toBeHidden();

  // And back — same iframe, visible again (no reload of the element).
  await page.locator(".pl-rail").getByRole("button", { name: "Chat", exact: true }).click();
  await expect(slotFrame).toBeVisible();
});

test("without a claimant the built-in chat renders (default unchanged)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".chat-tabbar")).toBeVisible();
  await expect(page.locator(".chat-slot")).toHaveCount(0);
});
