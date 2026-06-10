import { expect, test } from "@playwright/test";

// The Inbox lives under Activity (ADR 0003): inbound items, a sub-tab unread
// badge, live updates on `inbox.item`, and dismiss.

test("inbox badge appears, panel lists items, and dismiss removes one", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Inbound events bump the Activity rail's combined unread badge.
  await expect(page.locator(".pl-count--rail")).toBeVisible();

  // Open Activity → its Inbox sub-tab.
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("tab", { name: /Inbox/ }).click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

  // Items from GET /api/inbox render with priority + source.
  await expect(page.getByText("build failed on main")).toBeVisible();
  await expect(page.getByText("new signup: acme.co")).toBeVisible();
  await expect(page.locator(".inbox-pri-now")).toBeVisible();

  // Viewing the Inbox sub-tab clears its unread badge.
  await expect(page.getByTestId("inbox-badge")).toHaveCount(0);

  // Dismiss the first item → it leaves the list.
  const firstItem = page.locator(".inbox-item", { hasText: "build failed on main" });
  await firstItem.locator(".inbox-dismiss").click();
  await expect(page.getByText("build failed on main")).toHaveCount(0);
});
