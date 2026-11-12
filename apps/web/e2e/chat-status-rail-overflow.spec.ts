import { expect, test } from "@playwright/test";

// A long background-job label must never set the chat column's minimum width. The status
// rail's chip carries only the group name and count (S6a) — the label lives in the grouped
// details panel, where the row title ellipsizes inside the panel. Before the rail, the
// one-line strip's unwrapped label pushed the whole chat wider than the panel
// (Josh, 2026-10-04); this keeps that class of regression pinned.

const SESSION = "chat-bgwork-overflow-e2e";
const DESCRIPTION =
  "delegate → protoEngineer: File a protoAgent issue for reliable browser form-filling, decompose it into " +
  "features, board every feature with acceptance criteria, link the parent epic, and report back with the ids";

test("a long background-job label stays inside the details panel instead of widening the chat column", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 });
  await page.addInitScript(
    ([session]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "lead", createdAt: 1, updatedAt: 2, messages: [] }],
        }),
      );
    },
    [SESSION],
  );
  await page.route("**/api/background", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        jobs: [{ id: "bg-overflow0001", status: "running", origin_session: SESSION, subagent_type: "delegate", description: DESCRIPTION }],
      },
    }),
  );
  await page.route("**/api/background/bg-*", (route) => route.fulfill({ status: 404, json: { detail: "no job" } }));
  await page.route("**/api/events**", (route) =>
    route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }),
  );

  await page.goto("/app/", { waitUntil: "load" });
  const chip = page.getByTestId("chat-status-chip-doing-now");
  await expect(chip).toHaveText("Doing now 1");

  // Nothing overflows the chat column: the chip never carries the label.
  const m = await page.evaluate(() => {
    const slot = document.querySelector(".chat-session-slot:not([hidden])") as HTMLElement;
    const wrap = slot.querySelector(".composer-wrap") as HTMLElement;
    return {
      slotScroll: slot.scrollWidth,
      slotClient: slot.clientWidth,
      wrap: wrap.getBoundingClientRect().width,
    };
  });
  expect(m.slotScroll).toBeLessThanOrEqual(m.slotClient + 1);
  expect(m.wrap).toBeLessThanOrEqual(m.slotClient + 1);

  // The label is one click away, ellipsized inside the panel — the title is the thing that
  // gives (its content is wider than its box), and the panel stays inside the viewport.
  await chip.click();
  const panel = page.locator(".chat-status-details");
  await expect(panel).toContainText("File a protoAgent issue");
  const d = await page.evaluate(() => {
    const title = document.querySelector(".chat-status-details__title") as HTMLElement;
    const details = document.querySelector(".chat-status-details") as HTMLElement;
    return {
      titleScroll: title.scrollWidth,
      titleClient: title.clientWidth,
      panelRight: details.getBoundingClientRect().right,
      viewport: window.innerWidth,
    };
  });
  expect(d.titleScroll).toBeGreaterThan(d.titleClient);
  expect(d.panelRight).toBeLessThanOrEqual(d.viewport + 1);
});
