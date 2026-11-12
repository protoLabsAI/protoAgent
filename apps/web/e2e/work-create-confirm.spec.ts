import { expect, test, type Locator, type Page } from "@playwright/test";

// S8d acceptance (work-consolidation §10.8): at 390×844 the step-2 confirm ("Check this
// before saving") and the inspectors' Edit are the SAME bottom sheet the New flow uses —
// never a centered Dialog — with no sideways overflow (§10.2 layout rules), human Repeat
// choices (never cron), and the full-width stacked ≥44px footer, primary on top. Step 2 is
// reachable for all four readings (job, time, condition, instruction) — the mock backend
// answers the marked interpret turn statelessly from the message text.

const LONGEST = "Check #874 every 30 min and rebase it when #868 merges";
const URLISH = `https://ci.example.com/protoAgent/pulls/874/checks?run=${"a1b2c3d4e5".repeat(14)}`;

async function assertSheetGeometry(page: Page, sheet: Locator) {
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
}

async function assertNoOverflow(sheet: Locator) {
  const shape = await sheet.evaluate((el) => {
    const edge = el.getBoundingClientRect();
    const past = Array.from(el.querySelectorAll("*")).filter((child) => {
      const b = child.getBoundingClientRect();
      return b.width > 0 && (b.right > edge.right + 0.5 || b.left < edge.left - 0.5);
    }).map((child) => child.className?.toString() || child.tagName);
    return { past, scrollW: el.scrollWidth, clientW: el.clientWidth };
  });
  expect(shape.past).toEqual([]);
  expect(shape.scrollW).toBe(shape.clientW);
}

async function assertStackedFooter(sheet: Locator, primary: string) {
  const first = (await sheet.getByRole("button", { name: primary, exact: true }).boundingBox())!;
  const cancel = (await sheet.getByRole("button", { name: "Cancel", exact: true }).boundingBox())!;
  const foot = (await sheet.locator(".pl-drawer__foot").boundingBox())!;
  expect(first.height).toBeGreaterThanOrEqual(44);
  expect(cancel.height).toBeGreaterThanOrEqual(44);
  expect(first.y + first.height).toBeLessThanOrEqual(cancel.y + 0.5); // primary on top
  expect(first.width).toBeGreaterThan(foot.width - 40); // full width
}

async function openWorkSheet(page: Page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/app/");
  await page.getByRole("button", { name: "Open Work", exact: true }).click();
  const work = page.locator(".work-view:visible");
  await expect(work).toHaveAttribute("data-frame", "sheet");
  return work;
}

async function toConfirmStep(page: Page, work: Locator, words: string) {
  await work.getByRole("button", { name: "New", exact: true }).click();
  const sheet = page.locator(".pl-drawer.work-create--sheet");
  await sheet.locator("textarea").fill(words);
  await sheet.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(sheet.locator(".pl-drawer__title")).toHaveText("Check this before saving");
  return sheet;
}

test("phone 390: the condition confirm step is the bottom sheet with the editable fields, no overflow, stacked footer", async ({ page }, info) => {
  const work = await openWorkSheet(page);
  await work.getByRole("button", { name: "New", exact: true }).click();
  const sheet = page.locator(".pl-drawer.work-create--sheet");
  await sheet.getByRole("button", { name: LONGEST }).click();
  await sheet.getByRole("button", { name: "Continue", exact: true }).click();

  // Step 2 is the SAME bottom sheet (r1): full width, on the bottom edge, never a Dialog.
  await expect(sheet.locator(".pl-drawer__title")).toHaveText("Check this before saving");
  await assertSheetGeometry(page, sheet);
  await expect(sheet.getByText("Goes in Coming up as Condition.")).toBeVisible();

  // The §10.4 editable fields, prefilled from the reading — and no raw cron anywhere.
  for (const label of ["When", "Check with", "Check every", "Then", "Runs", "Stop checking after"]) {
    await expect(sheet.locator(".pl-field__label").filter({ hasText: label }).first()).toBeVisible();
  }
  await expect(sheet.locator(".work-create__body")).not.toContainText("* *");

  // r2/r3: nothing extends past either sheet edge — also with URL-length unbroken text.
  await assertNoOverflow(sheet);
  await sheet.locator("textarea").first().fill(URLISH);
  await assertNoOverflow(sheet);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  // r4: footer full width, stacked, ≥44px, primary (Save) above Cancel.
  await assertStackedFooter(sheet, "Save");

  // The 8d PR's 390 evidence: the condition confirm step (§10.8 point 6).
  const shot = info.outputPath("phone-390-condition-confirm.png");
  await page.screenshot({ path: shot });
  await info.attach("Condition confirm at 390", { path: shot, contentType: "image/png" });
});

test("phone 390: step 2 and an inspector Edit open as the sheet for all four readings — job, time, condition, instruction — without overflow", async ({ page }) => {
  const work = await openWorkSheet(page);
  const cases = [
    { words: "Re-run the design-system audit on protoContent", tag: "Goes in Doing now as Job.", primary: "Start", field: "Who" },
    { words: "Every weekday at 9, summarise what merged overnight", tag: "Goes in Coming up as Time.", primary: "Save", field: "Repeat" },
    { words: LONGEST, tag: "Goes in Coming up as Condition.", primary: "Save", field: "Check with" },
    { words: "Merge only when the review says PASS", tag: "Goes in Your instructions as Instruction.", primary: "Save", field: "Your words" },
  ];
  for (const item of cases) {
    const sheet = await toConfirmStep(page, work, item.words);
    await assertSheetGeometry(page, sheet);
    await expect(sheet.getByText(item.tag)).toBeVisible();
    await expect(sheet.locator(".pl-field__label").filter({ hasText: item.field }).first()).toBeVisible();
    await assertNoOverflow(sheet);
    await assertStackedFooter(sheet, item.primary);
    await sheet.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(sheet).toHaveCount(0);
  }
  // The instruction confirm shows the TYPED words read-only — the model's paraphrase
  // ("as the agent read it", see mock-server) must never reach the quote.
  const sheet = await toConfirmStep(page, work, "Merge only when the review says PASS");
  await expect(sheet.locator(".work-create__quote")).toHaveText("Merge only when the review says PASS");
  await sheet.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(sheet).toHaveCount(0);

  // An inspector Edit opens the SAME prefilled bottom sheet — "Edit follow-up", no
  // model call, the same geometry/overflow/footer rules as step 2.
  await work.locator(".work-row").filter({ hasText: "CI is green on main" }).first().click();
  await work.getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.locator(".pl-drawer.work-create--sheet");
  await expect(edit.locator(".pl-drawer__title")).toHaveText("Edit follow-up");
  await assertSheetGeometry(page, edit);
  await expect(edit.locator(".pl-field").filter({ hasText: "When" }).locator("input").first())
    .toHaveValue("CI is green on main");
  await assertNoOverflow(edit);
  await assertStackedFooter(edit, "Save changes");
});

test("window 1200: the condition confirm step stays a centered dialog with the same fields", async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto("/app/");
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  const work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
  await work.getByRole("button", { name: "New", exact: true }).click();
  const dialog = page.locator(".pl-dialog.work-create");
  await dialog.getByRole("button", { name: LONGEST }).click();
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(dialog.locator(".pl-dialog__title")).toHaveText("Check this before saving");
  await expect(page.locator(".pl-drawer.work-create--sheet")).toHaveCount(0);
  await expect(dialog.getByText("Goes in Coming up as Condition.")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
});
