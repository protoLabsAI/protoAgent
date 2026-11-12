import { expect, test, type Page } from "@playwright/test";

// S8c acceptance (work-consolidation §10.8, points 1–4): at a 390×844 viewport the New flow
// is the bottom sheet — never a centered Dialog — and nothing overflows sideways with the
// longest example chip filled in, nor with URL-length unbroken text. The bug that prompted
// this: a nowrap chip widened a bare 1fr grid track, clipping the intro, box and buttons.

const LONGEST = "Check #874 every 30 min and rebase it when #868 merges";
const URLISH = `https://ci.example.com/protoAgent/pulls/874/checks?run=${"a1b2c3d4e5".repeat(14)}`;

async function openWorkWindow(page: Page) {
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  await expect(page.getByRole("dialog", { name: /^Work/ })).toBeVisible();
}

test("phone 390: New is the bottom sheet and nothing overflows with the longest chip or URL-length text", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/app/");
  await page.getByRole("button", { name: "Open Work", exact: true }).click();
  const work = page.locator(".work-view:visible");
  await expect(work).toHaveAttribute("data-frame", "sheet");
  await work.getByRole("button", { name: "New", exact: true }).click();

  // 1. A full-width bottom sheet at the bottom of the viewport, not a centered Dialog.
  const sheet = page.locator(".pl-drawer.work-create--sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet).toHaveClass(/pl-drawer--bottom/);
  await expect(page.locator(".pl-dialog.work-create")).toHaveCount(0);
  // Client coordinates, polled until the slide-in animation lands the sheet on the
  // viewport's bottom edge (boundingBox mid-animation reads the transform's position).
  const geo = () => sheet.evaluate((el) => {
    const edge = el.getBoundingClientRect();
    return { left: edge.left, width: edge.width, bottom: edge.bottom, viewport: window.innerHeight };
  });
  await expect.poll(async () => Math.round((await geo()).bottom)).toBe(844);
  const placed = await geo();
  expect(placed.left).toBe(0);
  expect(placed.width).toBe(390);
  expect(Math.round(placed.bottom)).toBe(placed.viewport);

  // 2. Longest chip into the box, then unbroken URL-length text: no descendant past either
  //    sheet edge, and the sheet's scrollWidth equals its clientWidth.
  await sheet.getByRole("button", { name: LONGEST }).click();
  await expect(sheet.locator("textarea")).toHaveValue(LONGEST);
  const overflow = () => sheet.evaluate((el) => {
    const edge = el.getBoundingClientRect();
    const past = Array.from(el.querySelectorAll("*")).filter((child) => {
      const b = child.getBoundingClientRect();
      return b.width > 0 && (b.right > edge.right + 0.5 || b.left < edge.left - 0.5);
    }).map((child) => child.className?.toString() || child.tagName);
    return { past, scrollW: el.scrollWidth, clientW: el.clientWidth };
  });
  let shape = await overflow();
  expect(shape.past).toEqual([]);
  expect(shape.scrollW).toBe(shape.clientW);
  await sheet.locator("textarea").fill(URLISH);
  shape = await overflow();
  expect(shape.past).toEqual([]);
  expect(shape.scrollW).toBe(shape.clientW);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  // 3. The textarea fits the content box; every chip shows its full text, wrapped, unclipped.
  const body = (await sheet.locator(".pl-drawer__body").boundingBox())!;
  const ta = (await sheet.locator("textarea").boundingBox())!;
  expect(ta.x >= body.x && ta.x + ta.width <= body.x + body.width + 0.5).toBe(true);
  const chips = await sheet.locator(".pl-chip-label").evaluateAll((els) => els.map((el) => ({
    clipped: el.scrollWidth > el.clientWidth,
    wraps: getComputedStyle(el).whiteSpace === "normal",
  })));
  expect(chips).toHaveLength(4);
  expect(chips.filter((chip) => chip.clipped || !chip.wraps)).toEqual([]);

  // 4. Footer actions: full width, stacked, ≥44px tall, primary above Cancel.
  const go = (await sheet.getByRole("button", { name: "Continue", exact: true }).boundingBox())!;
  const cancel = (await sheet.getByRole("button", { name: "Cancel", exact: true }).boundingBox())!;
  const foot = (await sheet.locator(".pl-drawer__foot").boundingBox())!;
  expect(go.height).toBeGreaterThanOrEqual(44);
  expect(cancel.height).toBeGreaterThanOrEqual(44);
  expect(go.y + go.height).toBeLessThanOrEqual(cancel.y + 0.5);
  expect(go.width).toBeGreaterThan(foot.width - 40);
  expect(Math.abs(go.width - cancel.width)).toBeLessThan(1);

  // The PR's 390 evidence: the sheet with the longest chip filled in (§10.8 point 6).
  await sheet.locator("textarea").fill(LONGEST);
  const shot = info.outputPath("phone-390-new-sheet.png");
  await page.screenshot({ path: shot });
  await info.attach("New sheet at 390", { path: shot, contentType: "image/png" });
});

test("phone 390: the window route (palette Open Work) also gets the bottom sheet, never a centered Dialog", async ({ page }) => {
  // The rail/window split only exists on desktop — at 390 BOTH Work routes resolve to the
  // full-screen sheet host, and §10.2 says the New flow is the bottom Drawer on every one
  // of them. The first test drives the chat-root handle; this one drives the `surface`
  // intent (the palette's Open Work), the same route the desktop window uses.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/app/");
  await page.getByRole("button", { name: "Search commands" }).click();
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  const work = page.locator(".work-view:visible");
  await expect(work).toHaveAttribute("data-frame", "sheet");
  await work.getByRole("button", { name: "New", exact: true }).click();

  const sheet = page.locator(".pl-drawer.work-create--sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet).toHaveClass(/pl-drawer--bottom/);
  await expect(page.locator(".pl-dialog.work-create")).toHaveCount(0);
  const geo = () => sheet.evaluate((el) => {
    const edge = el.getBoundingClientRect();
    return { left: edge.left, width: edge.width, bottom: edge.bottom };
  });
  await expect.poll(async () => Math.round((await geo()).bottom)).toBe(844);
  const placed = await geo();
  expect(placed.left).toBe(0);
  expect(placed.width).toBe(390);
  // The longest chip must not widen this host either.
  await sheet.getByRole("button", { name: LONGEST }).click();
  await expect(sheet.locator("textarea")).toHaveValue(LONGEST);
  const shape = await sheet.evaluate((el) => ({ scrollW: el.scrollWidth, clientW: el.clientWidth }));
  expect(shape.scrollW).toBe(shape.clientW);
});

test("window: New opens the centered step-1 dialog; Escape returns focus to its opener", async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto("/app/");
  await openWorkWindow(page);
  const work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
  const newButton = work.getByRole("button", { name: "New", exact: true });
  await newButton.click();
  const dialog = page.locator(".pl-dialog.work-create");
  await expect(dialog).toBeVisible();
  await expect(page.locator(".pl-drawer.work-create--sheet")).toHaveCount(0);
  await expect(dialog.locator(".pl-dialog__title")).toHaveText("New");
  await expect(dialog.locator("textarea")).toBeFocused();
  // Continue stays disabled while the box is blank; a chip fills it and enables Continue.
  await expect(dialog.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: LONGEST }).click();
  await expect(dialog.getByRole("button", { name: "Continue", exact: true })).toBeEnabled();
  // Escape closes only the step-1 dialog (the Work window stays) and refocuses the opener.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: /^Work/ })).toBeVisible();
  await expect(newButton).toBeFocused();
});
