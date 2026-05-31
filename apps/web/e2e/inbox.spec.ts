import { expect, test } from "@playwright/test";

// The Inbox sidebar panel (ADR 0003): lists pending inbound items, shows an
// unread badge while off-panel, live-updates on `inbox.item`, and dismisses.

test("inbox badge appears, panel lists items, and dismiss removes one", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // inbox.item events arrive while the default (Notes) panel is showing → badge.
  await expect(page.getByTestId("inbox-badge")).toBeVisible();

  // Open the Inbox tab (its accessible name includes the badge count).
  await page.getByRole("button", { name: /Inbox/ }).click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

  // Items from GET /api/inbox render with priority + source.
  await expect(page.getByText("build failed on main")).toBeVisible();
  await expect(page.getByText("new signup: acme.co")).toBeVisible();
  await expect(page.locator(".inbox-pri-now")).toBeVisible();

  // Opening the panel clears the badge.
  await expect(page.getByTestId("inbox-badge")).toHaveCount(0);

  // Dismiss the first item → it leaves the list.
  const firstItem = page.locator(".inbox-item", { hasText: "build failed on main" });
  await firstItem.locator(".inbox-dismiss").click();
  await expect(page.getByText("build failed on main")).toHaveCount(0);
});
