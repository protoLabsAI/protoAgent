import { expect, test } from "@playwright/test";

// Inline chat components (ADR 0051 / #1323): show_component emits a component-v1 DataPart
// that the console's extensible registry renders inline — below the answer, in order, with
// the show_component tool card suppressed (it's a render directive, not work to fold).

test("show_component renders an inline table; its tool card is suppressed (#1323)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("COMPONENT: show the fleet");
  await composer.press("Enter");

  const msg = page.locator(".pl-message--assistant").first();

  // The table renders inline via the component registry.
  const comp = msg.locator(".chat-comp").first();
  await expect(comp).toBeVisible();
  await expect(comp).toContainText("Fleet"); // title
  await expect(comp).toContainText("Ship");
  await expect(comp).toContainText("Status");
  await expect(comp).toContainText("Hauler");
  await expect(comp).toContainText("active");

  // The show_component tool card is SUPPRESSED — no work-timeline noise.
  await expect(msg.locator(".pl-toolcard")).toHaveCount(0);

  // Ordering (#1323): the component renders ABOVE the answer text (it's emitted first, as an
  // ordered part), so the text streams in UNDER it — not shoved below.
  await expect(msg.getByText("Here's the fleet breakdown.")).toBeVisible();
  const order = await msg.evaluate((el) => {
    const c = el.querySelector(".chat-comp");
    const t = [...el.querySelectorAll(".markdown")].find((m) => /fleet breakdown/i.test(m.textContent || ""));
    return c && t && c.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING ? "component-above-text" : "other";
  });
  expect(order).toBe("component-above-text");
});
