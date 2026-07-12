import { expect, test } from "@playwright/test";

// #1973: a HITL interrupt (ask_human free-text / request_user_input wizard) must
// FLOAT above the chat — a card pinned over the composer — instead of rendering
// in-flow and reflowing the conversation. These specs pin the geometry contract:
// the conversation scroll container, previously rendered messages, and the
// composer all keep their EXACT bounding boxes while the form appears, is
// walked/answered, and disappears; and the chat stays readable + scrollable
// behind the card (deliberately no backdrop — answering usually means re-reading
// the conversation).

const SLOT = ".chat-session-slot:not([hidden])";

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

// Seed one settled turn so there is existing content whose geometry could shift.
async function seedTurn(page) {
  await send(page, "what is the capital of France?");
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    "Done — found 8 results.",
  );
}

test.beforeEach(async ({ page }) => {
  // Tall viewport: the whole exchange must fit WITHOUT the convo overflowing, so
  // stick-to-bottom scrolling never moves the seed message — any bounding-box
  // change below is then a true layout shift, not scroll.
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("ask_human floats above the composer with zero layout shift (#1973)", async ({ page }) => {
  await seedTurn(page);

  const convo = page.locator(`${SLOT} .pl-convo-scroll`);
  const prompt = page.locator(`${SLOT} .pl-prompt`);
  const seedMsg = page.locator(`${SLOT} .pl-message--user`).first();
  const convoBefore = await convo.boundingBox();
  const promptBefore = await prompt.boundingBox();
  const msgBefore = await seedMsg.boundingBox();

  await send(page, "HITL_ASK: deploy the release");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();
  await expect(card).toContainText("Which environment should I deploy to");

  // Zero layout shift: container, composer, and an existing message keep their
  // exact geometry while the card is up.
  expect(await convo.boundingBox()).toEqual(convoBefore);
  expect(await prompt.boundingBox()).toEqual(promptBefore);
  expect(await seedMsg.boundingBox()).toEqual(msgBefore);

  // The card OVERLAYS the conversation's bottom edge and sits above the composer
  // — floating, not wedged between them as a flow row.
  const cardBox = await card.boundingBox();
  expect(cardBox!.y + cardBox!.height).toBeLessThanOrEqual(promptBefore!.y + 1);
  expect(cardBox!.y).toBeLessThan(convoBefore!.y + convoBefore!.height);

  // No backdrop: the conversation stays readable and scrollable behind the card.
  await convo.evaluate((el) => {
    el.scrollTop = 0;
  });
  await expect(seedMsg).toBeVisible();
  await expect(card).toBeVisible();

  // Answer via the card → it resolves, the turn resumes, still no reflow.
  await card.locator("textarea").fill("staging");
  await card.getByRole("button", { name: "Send" }).click();
  await expect(card).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).last()).toContainText("staging");
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    "Done — found 8 results.",
  );
  expect(await convo.boundingBox()).toEqual(convoBefore);
  expect(await prompt.boundingBox()).toEqual(promptBefore);
  expect(await seedMsg.boundingBox()).toEqual(msgBefore);
});

test("request_user_input wizard floats and walks steps without reflow (#1973)", async ({ page }) => {
  await seedTurn(page);

  const convo = page.locator(`${SLOT} .pl-convo-scroll`);
  const prompt = page.locator(`${SLOT} .pl-prompt`);
  const convoBefore = await convo.boundingBox();
  const promptBefore = await prompt.boundingBox();

  await send(page, "HITL_FORM: gather deployment details");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();
  await expect(card).toContainText("Deployment details");
  await expect(card).toContainText("Step 1 / 2");

  expect(await convo.boundingBox()).toEqual(convoBefore);
  expect(await prompt.boundingBox()).toEqual(promptBefore);

  // Required gating still works in the floating card: Next unlocks on fill.
  const next = card.getByRole("button", { name: "Next" });
  await expect(next).toBeDisabled();
  await card.locator("input[type='text']").fill("staging");
  await next.click();
  await expect(card).toContainText("Step 2 / 2");

  // Changing steps (variable card height) doesn't reflow the chat either —
  // the card is out of flow.
  expect(await convo.boundingBox()).toEqual(convoBefore);
  expect(await prompt.boundingBox()).toEqual(promptBefore);

  await card.getByRole("button", { name: "Submit" }).click();
  await expect(card).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    "Done — found 8 results.",
  );
  expect(await convo.boundingBox()).toEqual(convoBefore);
  expect(await prompt.boundingBox()).toEqual(promptBefore);
});

test("the floating form stays inside a phone viewport (#1973)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/app/", { waitUntil: "load" });

  await send(page, "HITL_FORM: gather deployment details");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();

  // Fits the viewport width and caps its height (dvh) so the composer below
  // stays reachable.
  const cardBox = await card.boundingBox();
  const promptBox = await page.locator(`${SLOT} .pl-prompt`).boundingBox();
  expect(cardBox!.x).toBeGreaterThanOrEqual(0);
  expect(cardBox!.width).toBeLessThanOrEqual(390);
  expect(cardBox!.y).toBeGreaterThanOrEqual(0);
  expect(cardBox!.y + cardBox!.height).toBeLessThanOrEqual(promptBox!.y + 1);
});
