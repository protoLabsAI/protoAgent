import { expect, test } from "@playwright/test";

test("a fresh browser recovers a server-known chat through durable turn replay", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-session-history": "1" });
  await page.goto("/app/", { waitUntil: "load" });

  // One operator bubble: the second durable turn was server-fired, and the machine prompt
  // that fired it was never a bubble in the live chat — only its answer was.
  await expect(page.locator(".pl-message--user")).toHaveCount(1);
  await expect(page.locator(".pl-message--user")).toContainText("Recover this conversation");
  const answers = page.locator(".pl-message--assistant .markdown");
  await expect(answers).toHaveCount(2);
  await expect(answers.nth(0)).toContainText("The durable answer is back.");
  await expect(answers.nth(1)).toContainText("Scheduled check: the deploy is green.");
  await expect(page.getByText("Autonomous wake")).toHaveCount(0);
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
  await expect(page.locator(".pl-tabbar__tab")).toContainText("Recover this conversation");

  const persisted = await page.evaluate(() => JSON.parse(localStorage.getItem("protoagent.chat.sessions") || "{}"));
  expect(persisted.sessions.map((session: { id: string }) => session.id)).toEqual(["chat-recovered"]);
});
