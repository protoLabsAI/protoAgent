import { expect, test } from "@playwright/test";

// #1978: HitlForm defaults + keyboard contract for AGENT-driven interrupts (the /model
// slash-picker side lives in model-command.spec.ts). The floating card takes focus on
// appear (the default-selected choice card when one exists, else the first control),
// schema `default`s prefill answers — a default IS an answer, so required-with-default
// doesn't gate Submit — ←/→ move card selection, Enter confirms, Esc dismisses, and
// focus returns to the composer on close.

const SLOT = ".chat-session-slot:not([hidden])";

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("a defaults-carrying agent form opens prefilled + focused and submits untouched (#1978)", async ({ page }) => {
  await send(page, "HITL_FORM_PREFILL: confirm the deployment plan");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();

  // Prefill: the text field carries its default, the proposed card is selected, and —
  // because a default IS an answer — Submit is live with nothing touched.
  await expect(card.locator("input[type='text']")).toHaveValue("staging");
  const rolling = card.getByRole("radio", { name: /Rolling/ });
  await expect(rolling).toHaveAttribute("aria-checked", "true");
  await expect(card.getByRole("button", { name: "Submit" })).toBeEnabled();

  // Focus lands on the form's first control (the prefilled text field), and since a
  // default IS an answer the gates are already open — bare Enter confirms as-is.
  await expect(card.locator("input[type='text']")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(card).toHaveCount(0);

  // The untouched defaults ride back as the answer; the composer regains focus.
  const answer = page.locator(`${SLOT} .pl-message--user`).last();
  await expect(answer).toContainText("staging");
  await expect(answer).toContainText("rolling");
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    "Done — found 8 results.",
  );
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeFocused();
});

test("Tab enters the card group on the SELECTED card; arrows move the selection (#1978)", async ({ page }) => {
  await send(page, "HITL_FORM_PREFILL: confirm the deployment plan");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card.locator("input[type='text']")).toBeFocused();

  // Tab lands on the selected "Rolling" card — the SECOND option in DOM order — because
  // the roving tabindex parks the unselected cards at -1 (selection, not document
  // order, anchors the group).
  await page.keyboard.press("Tab");
  const rolling = card.getByRole("radio", { name: /Rolling/ });
  await expect(rolling).toBeFocused();

  // ← moves focus AND selection to the previous card (selection follows focus)…
  await page.keyboard.press("ArrowLeft");
  const blueGreen = card.getByRole("radio", { name: /Blue\/green/ });
  await expect(blueGreen).toBeFocused();
  await expect(blueGreen).toHaveAttribute("aria-checked", "true");
  await expect(rolling).toHaveAttribute("aria-checked", "false");

  // …and → moves it back.
  await page.keyboard.press("ArrowRight");
  await expect(rolling).toBeFocused();
  await expect(rolling).toHaveAttribute("aria-checked", "true");
});

test("a plain wizard focuses its first field, Enter advances, Esc dismisses to the composer (#1978)", async ({ page }) => {
  await send(page, "HITL_FORM: gather deployment details");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();

  // No defaults here → focus lands on the step's first control, and the required
  // field still gates (no behavior change for payloads without defaults).
  const env = card.locator("input[type='text']");
  await expect(env).toBeFocused();
  await expect(card.getByRole("button", { name: "Next" })).toBeDisabled();

  // Type + Enter = fill and advance (Enter shares the Next button's gating).
  await env.fill("staging");
  await page.keyboard.press("Enter");
  await expect(card).toContainText("Step 2 / 2");

  // Esc dismisses without answering; the turn resumes and the composer gets focus back.
  await page.keyboard.press("Escape");
  await expect(card).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    "Done — found 8 results.",
  );
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeFocused();
});
