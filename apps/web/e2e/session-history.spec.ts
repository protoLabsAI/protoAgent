import { expect, test } from "@playwright/test";

test("a fresh browser recovers a server-known chat through durable turn replay", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-session-history": "1" });
  await page.goto("/app/", { waitUntil: "load" });

  await expect(page.locator(".pl-message--user")).toContainText("Recover this conversation");
  await expect(page.locator(".pl-message--assistant .markdown")).toContainText("The durable answer is back.");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
  await expect(page.locator(".pl-tabbar__tab")).toContainText("Recover this conversation");

  const persisted = await page.evaluate(() => JSON.parse(localStorage.getItem("protoagent.chat.sessions") || "{}"));
  expect(persisted.sessions.map((session: { id: string }) => session.id)).toEqual(["chat-recovered"]);
});
