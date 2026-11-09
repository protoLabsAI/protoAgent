import { expect, test } from "@playwright/test";
import { persistedChat } from "./persistedChat";

test("a fresh browser recovers a server-known chat through durable turn replay", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-session-history": "1" });
  await page.goto("/app/", { waitUntil: "load" });

  // The operator's own messages come back: the prompt and the interjection the agent read
  // mid-turn. The later turns were server-fired and a hidden approval resume, neither of
  // which was ever a bubble in the live chat — only their answers were.
  const asked = page.locator(".pl-message--user");
  await expect(asked).toHaveCount(2);
  await expect(asked.nth(0)).toContainText("Recover this conversation");
  await expect(asked.nth(1)).toContainText("Also check the version");
  const answers = page.locator(".pl-message--assistant .markdown");
  await expect(answers).toHaveCount(3);
  await expect(answers.nth(0)).toContainText("The durable answer is back.");
  await expect(answers.nth(1)).toContainText("Scheduled check: the deploy is green.");
  await expect(answers.nth(2)).toContainText("Approved — the release is out.");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
  await expect(page.locator(".pl-tabbar__tab")).toContainText("Recover this conversation");

  // Saves to IndexedDB are async (#4981): poll until the hydrated chat has landed.
  await expect
    .poll(async () => ((await persistedChat(page))?.sessions ?? []).map((session) => session.id))
    .toEqual(["chat-recovered"]);
});
