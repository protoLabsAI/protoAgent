import { expect, test } from "@playwright/test";

// Plugin-view deep links (lib/viewDeepLink.ts): `?plugin=<id>&view=<view>&vp=<query>` on a
// console URL opens that plugin view on load and appends `vp` to the view's iframe src, so a
// QR label (inventory bins) lands on the right page with its params. The mock's "boardy"
// plugin declares Board (with tabs), Stats and a right-dock Scratch view.

const frame = (page: import("@playwright/test").Page) => page.locator(".plugin-view-frame");

test("a deep link opens the named view with its params, then leaves a clean address bar", async ({ page }) => {
  await page.goto("/app/?plugin=boardy&view=stats&vp=bin%3DB07%26from%3Dqr", { waitUntil: "load" });
  await expect(frame(page)).toHaveAttribute("src", /\/plugins\/boardy\/stats\?bin=B07&from=qr$/);
  await expect(page).toHaveURL(/\/app\/$/);
  // The page itself sees the params in its own location.search.
  await expect
    .poll(() =>
      page
        .frame({ url: /\/plugins\/boardy\/stats/ })
        ?.evaluate(() => new URLSearchParams(location.search).get("bin")),
    )
    .toBe("B07");
});

test("a view with tabs keeps its tab query and appends the link's", async ({ page }) => {
  await page.goto("/app/?plugin=boardy&view=board&vp=bin%3DB01", { waitUntil: "load" });
  await expect(frame(page)).toHaveAttribute("src", /\/plugins\/boardy\/board\?tab=open&bin=B01$/);
});

test("no view named → the plugin's first view", async ({ page }) => {
  await page.goto("/app/?plugin=boardy", { waitUntil: "load" });
  await expect(frame(page)).toHaveAttribute("src", /\/plugins\/boardy\/board\?tab=open$/);
});

test("a right-dock view opens in the right dock", async ({ page }) => {
  await page.goto("/app/?plugin=boardy&view=scratch&vp=a%3D1", { waitUntil: "load" });
  await expect(frame(page)).toHaveAttribute("src", /\/plugins\/boardy\/scratch\?a=1$/);
  // …in the RIGHT dock, not routed to the left one (the reconcile race this gate exists for).
  await expect(page.locator(".pl-rail--right").getByRole("button", { name: "Scratch", exact: true })).toBeVisible();
});

test("an unknown plugin leaves the normal console and says why", async ({ page }) => {
  await page.goto("/app/?plugin=nope&vp=bin%3DB07", { waitUntil: "load" });
  await expect(page.getByText("Couldn't open that link")).toBeVisible();
  await expect(page.getByText(/no “nope” view/)).toBeVisible();
  await expect(frame(page)).toHaveCount(0);
  await expect(page).toHaveURL(/\/app\/$/);
});

test("a malformed link is refused, not followed", async ({ page }) => {
  await page.goto("/app/?plugin=https%3A%2F%2Fevil.example&vp=x%3D1", { waitUntil: "load" });
  await expect(page.getByText("Couldn't open that link")).toBeVisible();
  await expect(frame(page)).toHaveCount(0);
});

test("other query params survive the strip", async ({ page }) => {
  await page.goto("/app/?keep=1&plugin=boardy&view=stats", { waitUntil: "load" });
  await expect(frame(page)).toHaveAttribute("src", /\/plugins\/boardy\/stats$/);
  await expect(page).toHaveURL(/\/app\/\?keep=1$/);
});

test("a fleet member's slug window opens the member's view through the hub proxy", async ({ page }) => {
  await page.goto("/app/agent/roxy/?plugin=boardy&view=stats&vp=bin%3DB07", { waitUntil: "load" });
  await expect(frame(page)).toHaveAttribute("src", /\/agents\/roxy\/plugins\/boardy\/stats\?bin=B07$/);
  await expect(page).toHaveURL(/\/app\/agent\/roxy\/$/);
});
