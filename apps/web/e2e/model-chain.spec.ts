import { expect, test } from "@playwright/test";

test("model chain ids wrap at 390px and phone controls retain touch targets", async ({ page }) => {
  const id = "anthropic-oauth:claude-haiku-5-5";
  await page.route("**/api/settings/schema*", (route) => route.fulfill({ json: { groups: [{
    category: "Model", section: "Model roles", fields: [
      { key: "model.name", label: "Main", role: "main", inherits: "", value: [id] },
      { key: "routing.aux_model", label: "Auxiliary", role: "aux", inherits: "main", value: [] },
    ].map((field) => ({ ...field, type: "model_chain", section: "Model roles",
      subhead: "Main and aux", options: [id], restart: false, scope: "agent", source: "agent" })),
  }] } }));
  await page.route("**/api/config/model-roles", (route) => route.fulfill({ json: { roles: [{
    role: "aux", chain: [id], inherited: true, source: "main", status: "ok",
    last_fallback: { time: new Date(Date.now() - 7200000).toISOString(), from: [id],
      to: "connection:extremely-long-winning-model-identifier-that-must-wrap-at-phone-width",
      reason: `${id}: RateLimitError (429)` },
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
  const fallback = page.getByRole("button", { name: "fell back 2h ago", exact: true });
  expect((await fallback.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await fallback.click();
  const statusPopover = page.locator(".model-role-status-popover");
  await expect(statusPopover).toContainText(id);
  expect(await statusPopover.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  const bounds = (await statusPopover.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  await page.keyboard.press("Escape");
  await expect(fallback).toBeFocused();
  const edit = main.getByRole("button", { name: "Edit Main chain", exact: true });
  expect((await edit.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await edit.click();
  await expect(main.getByRole("button", { name: "Change", exact: true })).toBeFocused();
  await main.getByRole("button", { name: "Change", exact: true }).click();
  expect((await main.getByRole("combobox").boundingBox())!.height).toBeGreaterThanOrEqual(44);
});

test("read-mode summary reads as one unbulleted wrapping line with a muted arrow", async ({ page }) => {
  const a = "anthropic-oauth:claude-haiku-5-5";
  const b = "connection:extremely-long-winning-model-identifier-that-must-wrap-at-phone-width";
  await page.route("**/api/settings/schema*", (route) => route.fulfill({ json: { groups: [{
    category: "Model", section: "Model roles", fields: [
      { key: "model.name", label: "Main", role: "main", inherits: "", value: [a, b] },
    ].map((field) => ({ ...field, type: "model_chain", section: "Model roles",
      subhead: "Main and aux", options: [a, b], restart: false, scope: "agent", source: "agent" })),
  }] } }));
  await page.route("**/api/config/model-roles", (route) => route.fulfill({ json: { roles: [] } }));
  await page.goto("/app/");
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  const accordion = page.getByRole("button", { name: "Model roles", exact: true });
  if (await accordion.getAttribute("aria-expanded") !== "true") await accordion.click();
  await page.setViewportSize({ width: 390, height: 844 });

  const main = page.getByRole("group", { name: "Main model chain", exact: true });
  const summary = main.locator(".model-chain__summary");
  await expect(summary).toBeVisible();

  // No bullets and no indent — the chain must not read as a bulleted list (criterion 1), and it
  // must not pick up the phone editor-row card chrome (criterion 3).
  const box = await summary.evaluate((el) => {
    const s = getComputedStyle(el);
    return { listStyleType: s.listStyleType, paddingLeft: s.paddingLeft, marginTop: s.marginTop,
      marginBottom: s.marginBottom, display: s.display, flexWrap: s.flexWrap,
      background: s.backgroundColor, borderTop: s.borderTopStyle };
  });
  expect(box.listStyleType).toBe("none");
  expect(box.paddingLeft).toBe("0px");
  expect(box.marginTop).toBe("0px");
  expect(box.marginBottom).toBe("0px");
  expect(box.display).toContain("flex");
  expect(box.flexWrap).toBe("wrap");
  expect(box.background).toBe("rgba(0, 0, 0, 0)");
  expect(box.borderTop).toBe("none");

  // The separator between the two ids is a muted arrow — the same token as the id prefix
  // (criteria 1 and 4: muted colour, design-system token).
  const sep = main.locator(".model-chain__summary-sep");
  await expect(sep).toHaveCount(1); // one arrow between two models
  const sepColor = await sep.evaluate((el) => getComputedStyle(el).color);
  const prefixColor = await main.locator(".model-chain__id-prefix").first()
    .evaluate((el) => getComputedStyle(el).color);
  expect(sepColor).toBe(prefixColor);

  // Every id fits and is never ellipsised at 390px, including the deliberately long one (criterion 2).
  const ids = main.locator(".model-chain__id");
  expect(await ids.count()).toBeGreaterThan(0);
  for (const element of await ids.all()) {
    const d = await element.evaluate((el) => ({ width: el.clientWidth, scroll: el.scrollWidth,
      overflow: getComputedStyle(el).textOverflow }));
    expect(d.width).toBeGreaterThan(0);
    expect(d.scroll).toBeLessThanOrEqual(d.width);
    expect(d.overflow).not.toBe("ellipsis");
  }

  // Edit keeps its 44px touch target (the existing phone rule) and hugs the start of the row.
  const edit = main.getByRole("button", { name: "Edit Main chain", exact: true });
  expect((await edit.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  expect(await edit.evaluate((el) => getComputedStyle(el).alignSelf)).toBe("flex-start");
});
