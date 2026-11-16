import { expect, test } from "@playwright/test";

test("model chain ids wrap at 390px and phone controls retain touch targets", async ({ page }) => {
  const id = "anthropic-oauth:claude-haiku-5-5";
  await page.route("**/api/settings/schema*", (route) => route.fulfill({ json: { groups: [{
    category: "Model", section: "Model roles", fields: [
      { key: "model.name", label: "Main", role: "main", inherits: "", value: [id] },
      { key: "routing.aux_model", label: "Auxiliary", role: "aux", inherits: "main", value: [] },
    ].map((field) => ({ ...field, type: "model_chain", section: "Model roles",
      options: [id], restart: false, scope: "agent", source: "agent" })),
  }] } }));
  await page.goto("/app/");
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  const accordion = page.getByRole("button", { name: "Model roles", exact: true });
  if (await accordion.getAttribute("aria-expanded") !== "true") await accordion.click();
  await page.setViewportSize({ width: 390, height: 844 });
  const main = page.getByRole("group", { name: "Main model chain", exact: true });
  await expect(main.locator(".model-chain__id-name")).toHaveText("claude-haiku-5-5");
  const assertFits = async () => {
    const ids = page.locator(".model-chain__id");
    expect(await ids.count()).toBeGreaterThan(0);
    for (const element of await ids.all()) {
      const dimensions = await element.evaluate((el) => ({
        width: el.clientWidth, scroll: el.scrollWidth, overflow: getComputedStyle(el).textOverflow,
      }));
      expect(dimensions.width).toBeGreaterThan(0);
      expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
      expect(dimensions.overflow).not.toBe("ellipsis");
    }
  };
  await assertFits();
  for (const button of await main.getByRole("button").all()) {
    expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  await page.getByRole("button", { name: "Inherits main", exact: true }).click();
  await expect(page.locator(".model-chain__popover-list")).toContainText(id);
  await assertFits();
  const popover = page.locator(".model-chain__popover");
  const inherited = page.getByRole("group", { name: "Auxiliary model chain", exact: true });
  expect((await popover.boundingBox())!.width).toBeLessThanOrEqual((await inherited.boundingBox())!.width);
  await page.keyboard.press("Escape");
  await main.getByRole("button", { name: "Change", exact: true }).click();
  expect((await main.getByRole("combobox").boundingBox())!.height).toBeGreaterThanOrEqual(44);
});
