import { expect, test } from "@playwright/test";

// The composer's model menu must stay inside the viewport and scroll (#3111). The DS
// caps its own DropSelect but never base `.pl-menu`, so a menu with more models than
// fit simply ran off the bottom of the screen with no way to reach the rest — which is
// every operator holding a gateway plus a subscription or two.
test("the model menu is capped to the viewport and scrolls", async ({ page }) => {
  await page.route("**/api/settings/schema", async (route) => {
    const json = await (await route.fetch()).json();
    const many = Array.from({ length: 36 }, (_, i) => `gateway:protolabs/model-${i + 1}`);
    for (const g of json.groups ?? [])
      for (const f of g.fields ?? []) {
        // Pinned favorites win over the full list (modelChoices), and the fixture pins
        // two — clear them so the long list is what renders.
        if (f.key === "model.favorites") { f.value = []; f.options = many; }
        if (f.key === "model.name") f.options = [...new Set([...(f.options ?? []), ...many])];
      }
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Model for this chat" }).click();

  const menu = page.locator(".pl-menu").first();
  await menu.waitFor();
  // Enough rows that an uncapped menu would overflow — otherwise this asserts nothing.
  expect(await page.locator(".pl-menu__item").count()).toBeGreaterThan(20);

  const box = (await menu.boundingBox())!;
  const viewportH = page.viewportSize()!.height;
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(viewportH + 1);

  // Capped is only useful if the overflow is REACHABLE — the bug was height without scroll.
  const { scrollHeight, clientHeight, overflowY } = await menu.evaluate((el) => ({
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    overflowY: getComputedStyle(el).overflowY,
  }));
  expect(scrollHeight).toBeGreaterThan(clientHeight);
  expect(["auto", "scroll"]).toContain(overflowY);
});
