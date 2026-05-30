import { expect, test } from "@playwright/test";

// Static-asset wiring. The favicon href is base-sensitive: a hardcoded "/app/"
// prefix double-prepends under Vite's dev base (→ "/app/app/…", 404). This
// guards that the icon link actually resolves so the tab favicon shows.

test("favicon link resolves to the icon asset", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "domcontentloaded" });

  const href = await page.locator('link[rel="icon"]').getAttribute("href");
  expect(href, "an icon link must be declared").toBeTruthy();

  // Resolve relative to the document URL the way the browser does, then fetch.
  const resolved = new URL(href as string, page.url()).toString();
  const resp = await page.request.get(resolved);
  expect(resp.status(), `favicon ${resolved} should resolve`).toBe(200);
  expect(resp.headers()["content-type"]).toContain("svg");
});
