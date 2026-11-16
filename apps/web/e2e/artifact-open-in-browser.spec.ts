import { expect, test, type Page, type Route } from "@playwright/test";
import { CHAIN_STORE } from "./artifactPanel";

// The Artifact panel's "Open in browser" button (#4939). It opens the version on screen as the
// same signed standalone page `open_artifact` hands the agent. The shell mints the link from the
// gated `POST …/artifact/{id}/page-link` AHEAD of the click, so the button is a real
// <a target=_blank>: a plain link click opens the tab in every host (the console's plugin iframe,
// the desktop app), where a window.open() after an awaited fetch can be popup-blocked.

const VIEW = "/plugins/artifact/view";
const T = "b".repeat(64);
const pagePath = (id: string, n: number) => `/plugins/artifact/media/artifact/${id}/page?version=${n}&exp=1900000000&t=${T}`;

async function mintRoute(page: Page, exp = () => 1_900_000_000) {
  const minted: string[] = [];
  await page.route("**/api/plugins/artifact/artifact/*/page-link?*", (route: Route) => {
    const url = new URL(route.request().url());
    const id = decodeURIComponent(url.pathname.split("/").at(-2) || "");
    const n = Number(url.searchParams.get("version"));
    minted.push(`${id}@${n}`);
    expect(route.request().method()).toBe("POST");
    return route.fulfill({ json: { path: pagePath(id, n), version: n, exp: exp() } });
  });
  return minted;
}

test("the button is a real link to the signed page for the version on screen", async ({ page, context }) => {
  const minted = await mintRoute(page);
  await page.goto(VIEW);
  const open = page.locator("#open");
  await expect(open).toBeVisible();
  await expect(open).toHaveText("Open in browser");
  await expect(open).toHaveAttribute("href", new RegExp(`${pagePath("art-doc", 1).replace(/[?]/g, "\\?")}$`));
  await expect(open).toHaveAttribute("target", "_blank");
  await expect(open).toHaveAttribute("rel", "noopener noreferrer");
  await expect(open).toHaveAttribute("aria-disabled", "false");
  expect(minted[0]).toBe("art-doc@1");

  await context.route("**/plugins/artifact/media/artifact/art-doc/page**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<title>Standalone</title><p id=page>standalone page</p>" }),
  );
  const opened = context.waitForEvent("page");
  await open.click();
  const tab = await opened;
  await expect(tab.locator("#page")).toHaveText("standalone page");
  expect(tab.url()).toContain("/plugins/artifact/media/artifact/art-doc/page?version=1");
  expect(tab.url()).not.toContain("token=");
  expect(await tab.evaluate(() => window.opener)).toBeNull();

  // A narrow dock folds it into the ⋯ menu with the other actions, still the armed link.
  await page.setViewportSize({ width: 420, height: 700 });
  await page.locator("#more").click();
  const item = page.locator("#moremenu #open");
  await expect(item).toBeVisible();
  await expect(item).toHaveAttribute("role", "menuitem");
  await expect(item).toHaveAttribute("href", /art-doc\/page/);
});

test("stepping versions re-arms the link for the version now shown", async ({ page }) => {
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: CHAIN_STORE }));
  const minted = await mintRoute(page);
  await page.goto(VIEW);
  const open = page.locator("#open");
  await expect(open).toHaveAttribute("href", /art-chain\/page\?version=3&/);
  await page.locator("#vprev").click();
  await expect(open).toHaveAttribute("href", /art-chain\/page\?version=2&/);
  expect(minted).toEqual(["art-chain@3", "art-chain@2"]);
});

test("a stale link is re-minted on click and still opens the tab", async ({ page, context }) => {
  // Every mint comes back already past its expiry, so the armed href is never trusted: the click
  // re-mints and falls back to window.open with the fresh link.
  const minted = await mintRoute(page, () => Math.floor(Date.now() / 1000) - 1);
  await page.goto(VIEW);
  const open = page.locator("#open");
  await expect(open).toHaveAttribute("href", /art-doc\/page/);
  await context.route("**/plugins/artifact/media/artifact/art-doc/page**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<p id=page>fresh</p>" }),
  );
  const before = minted.length;
  const opened = context.waitForEvent("page");
  await open.click();
  const tab = await opened;
  await expect(tab.locator("#page")).toHaveText("fresh");
  expect(minted.length).toBeGreaterThan(before);
  expect(await tab.evaluate(() => window.opener)).toBeNull();
});

test("a failed mint leaves no link and says so on the button", async ({ page }) => {
  await page.route("**/api/plugins/artifact/artifact/*/page-link?*", (route) => route.fulfill({ status: 404, json: {} }));
  await page.goto(VIEW);
  const open = page.locator("#open");
  await expect(open).toBeVisible();
  await expect(open).toHaveAttribute("aria-disabled", "true");
  await expect(open).not.toHaveAttribute("href", /.+/);
  await open.click();
  await expect(open).toHaveText("Unavailable");
  await expect(page.locator("#dlstat")).toHaveText("Couldn't open this version in a browser tab");
});
