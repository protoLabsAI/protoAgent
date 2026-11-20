import { expect, test, type Page, type Route } from "@playwright/test";
import { CHAIN_STORE } from "./artifactPanel";

// The Artifact panel's "Open in browser" action (#4939), in the toolbar's ⋯ menu at every width. It opens the version on screen as the
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

// The actions live in the ⋯ menu at every width: open it to reach one. Picking an action closes it.
async function openMenu(page: Page) {
  if ((await page.locator("#more").getAttribute("aria-expanded")) !== "true") await page.locator("#more").click();
  await expect(page.locator("#moremenu")).toBeVisible();
}

test("the button is a real link to the signed page for the version on screen", async ({ page, context }) => {
  const minted = await mintRoute(page);
  await page.goto(VIEW);
  const open = page.locator("#moremenu #open");
  await expect(page.locator("#more")).toBeVisible();
  await expect(open).toBeHidden(); // in the ⋯ menu, not on the bar
  await openMenu(page);
  await expect(open).toBeVisible();
  await expect(open).toHaveAttribute("role", "menuitem");
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

  // Picking it closed the menu. A narrow dock is the same layout, still the armed link.
  await expect(page.locator("#moremenu")).toBeHidden();
  await page.setViewportSize({ width: 420, height: 700 });
  await openMenu(page);
  await expect(open).toBeVisible();
  await expect(open).toHaveAttribute("role", "menuitem");
  await expect(open).toHaveAttribute("href", /art-doc\/page/);
});

for (const width of [1280, 420]) {
  test(`at ${width}px the toolbar is picker, version nav and ⋯, with every action in the menu`, async ({ page }) => {
    await mintRoute(page);
    await page.setViewportSize({ width, height: 720 });
    await page.goto(VIEW);
    await expect(page.locator("#art")).toBeVisible();
    await expect(page.locator("#vnav")).toBeVisible();
    const more = page.locator("#more");
    await expect(more).toBeVisible();
    await expect(more).toHaveAttribute("aria-expanded", "false");
    // No action sits on the bar: all four are inside the (closed) menu, and the fold is gone.
    for (const id of ["edit", "open", "dl", "del"]) {
      await expect(page.locator(`#moremenu #${id}`)).toHaveCount(1);
      await expect(page.locator(`#${id}`)).toBeHidden();
      await expect(page.locator(`#${id}`)).toHaveAttribute("role", "menuitem");
    }
    await expect(page.locator("#acts")).toHaveCount(0);
    await expect(page.locator("#bar")).not.toHaveClass(/compact/);
    const rows = await page.evaluate(() => new Set([...document.querySelectorAll("#bar > *")]
      .filter((e) => (e as HTMLElement).offsetParent !== null)
      .map((e) => { const r = e.getBoundingClientRect(); return Math.round((r.top + r.bottom) / 4); })).size);
    expect(rows).toBe(1);

    // ⋯ opens the menu on its first applicable item; the arrows walk the visible items and wrap.
    await more.click();
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("#dl")).toBeVisible();
    await expect(page.locator("#del")).toBeVisible();
    const visible = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>("#moremenu [role=menuitem]")]
      .filter((e) => e.style.display !== "none").map((e) => e.id));
    expect(visible.length).toBeGreaterThanOrEqual(2);
    const focused = () => page.evaluate(() => document.activeElement?.id);
    expect(await focused()).toBe(visible[0]);
    await page.keyboard.press("ArrowDown");
    expect(await focused()).toBe(visible[1]);
    await page.keyboard.press("ArrowUp");
    expect(await focused()).toBe(visible[0]);
    await page.keyboard.press("ArrowUp");
    expect(await focused()).toBe(visible[visible.length - 1]);

    // The first Delete click arms "Confirm?" and keeps the menu open; Escape closes onto ⋯.
    await page.locator("#del").click();
    await expect(page.locator("#del")).toHaveText("Confirm?");
    await expect(page.locator("#moremenu")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("#moremenu")).toBeHidden();
    await expect(more).toBeFocused();
    await expect(more).toHaveAttribute("aria-expanded", "false");

    // A click outside closes it too.
    await more.click();
    await expect(page.locator("#moremenu")).toBeVisible();
    await page.locator("#vlabel").click();
    await expect(page.locator("#moremenu")).toBeHidden();
  });
}

test("stepping versions re-arms the link for the version now shown", async ({ page }) => {
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: CHAIN_STORE }));
  const minted = await mintRoute(page);
  await page.goto(VIEW);
  const open = page.locator("#open");
  await expect(open).toHaveAttribute("href", /art-chain\/page\?version=3&/);
  await page.locator("#vprev").click();
  await expect(open).toHaveAttribute("href", /art-chain\/page\?version=2&/);
  expect(minted).toEqual(["art-chain@3", "art-chain@2"]);
  await openMenu(page);
  await expect(open).toBeVisible();
  await expect(open).toHaveAttribute("role", "menuitem");
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
  await openMenu(page);
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
  await openMenu(page);
  await expect(open).toBeVisible();
  await expect(open).toHaveAttribute("aria-disabled", "true");
  await expect(open).not.toHaveAttribute("href", /.+/);
  // Playwright won't click an aria-disabled menuitem; a real click still reaches the handler.
  await open.click({ force: true });
  await expect(open).toHaveText("Unavailable");
  await expect(page.locator("#dlstat")).toHaveText("Couldn't open this version in a browser tab");
});
