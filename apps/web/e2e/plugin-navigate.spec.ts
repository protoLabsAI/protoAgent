import { expect, test, type Page } from "@playwright/test";

// Plugin-driven navigation (ADR 0044) after a plugin publishes (#4283). A tool like Atlas's
// `atlas_show` publishes `<plugin>.show` and THEN `registry.navigate(view)`. Two cases:
//   • the view is already open: the navigate must leave its iframe alone (same document),
//     and the show arrives live;
//   • another surface holds its dock (e.g. `show_code` put the Code pane there): the
//     navigate mounts a FRESH page that subscribes after the show was published. The host's
//     navigate catch-up must still hand it that show, even to a page that subscribes
//     without `since`.
// The mock's bus tap (x-e2e-bus-tap) lets the spec fire the publish → navigate pair exactly
// when it wants; under a tap the mock page subscribes without `since` and records each
// `.show` id it receives in body[data-shows].

const TAP = `nav-${Math.random().toString(36).slice(2)}`;
test.use({ extraHTTPHeaders: { "x-e2e-bus-tap": TAP } });

async function showThenNavigate(page: Page, id: string) {
  const res = await page.request.post("/api/__test__/bus", {
    data: {
      tap: TAP,
      frames: [
        { topic: "boardy.show", data: { id } },
        { topic: "ui.navigate", data: { plugin: "boardy", view: "scratch" } },
      ],
    },
  });
  expect((await res.json()).streams).toBeGreaterThan(0);
}

async function openScratch(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail--right").getByRole("button", { name: "Scratch", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /\/plugins\/boardy\/scratch/);
  await expect(page.frameLocator(".plugin-view-frame").locator("body")).toHaveAttribute("data-bridge", /authed|anon/);
}

const scratchFrame = (page: Page) => page.frame({ url: /\/plugins\/boardy\/scratch/ });

test("ui.navigate to an already-open plugin view keeps its iframe, and the show arrives live", async ({ page }) => {
  await openScratch(page);
  // Stamp the live document: a remount or reload would give the iframe a fresh one.
  await scratchFrame(page)!.evaluate(() => ((window as unknown as { __mark: string }).__mark = "same-document"));

  await showThenNavigate(page, "s1");
  await showThenNavigate(page, "s2");

  const body = page.frameLocator(".plugin-view-frame").locator("body");
  await expect(body).toHaveAttribute("data-shows", "s1,s2");
  await expect(page.locator(".plugin-view-frame")).toHaveCount(1);
  expect(await scratchFrame(page)!.evaluate(() => (window as unknown as { __mark?: string }).__mark)).toBe(
    "same-document",
  );
});

test("ui.navigate that MOUNTS the view (another surface held its dock) still delivers the show published just before", async ({
  page,
}) => {
  await openScratch(page);
  // A show the first page already received; the catch-up must not hand it over twice.
  await showThenNavigate(page, "seen");
  await expect(page.frameLocator(".plugin-view-frame").locator("body")).toHaveAttribute("data-shows", "seen");

  // Another right-dock surface takes the dock: the Scratch page unmounts.
  await page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveCount(0);

  // publish → navigate: the navigate mounts a fresh page AFTER the show crossed the bus.
  await showThenNavigate(page, "fresh");
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /\/plugins\/boardy\/scratch/);
  await expect(page.frameLocator(".plugin-view-frame").locator("body")).toHaveAttribute("data-shows", "fresh");
});
