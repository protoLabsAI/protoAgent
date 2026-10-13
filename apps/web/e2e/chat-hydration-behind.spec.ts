import { expect, test } from "@playwright/test";

// A chat this browser had cached must not hide turns another client ran in it later.
// Browser B opened chat X while A's turn was running and cached it; A then ran more turns
// (one stopped). B's non-empty local copy used to win on every reload — boot hydration
// skipped any session with local messages — so B never showed A's later turns, however
// often it reloaded, though the server had them all. Boot now reads the turns of a cached
// session the server counts more turns for, adds the ones after its newest, and keeps its
// own render of the turns it already had.
//
// The mock (x-e2e-session-behind) serves chat-behind with three turns; B's cache holds the
// first, rendered with text only this browser ever drew.

const SLOT = ".chat-session-slot:not([hidden])";
const LOCAL_ANSWER = "The first answer, as this browser rendered it live.";

test("a reload shows the turns another browser ran after this one cached the chat", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-session-behind": "1" });
  await page.addInitScript((answer) => {
    // Seed B's cache once per tab (sessionStorage survives the reloads below).
    if (sessionStorage.getItem("seeded-behind")) return;
    sessionStorage.setItem("seeded-behind", "1");
    localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({
        version: 1,
        currentSessionId: "chat-behind",
        sessions: [{
          id: "chat-behind",
          title: "First question",
          createdAt: 1,
          updatedAt: 1,
          messages: [
            { id: "u-local-1", role: "user", content: "First question", status: "done" },
            { id: "a-local-1", role: "assistant", content: answer, status: "done", taskId: "task-behind-1" },
          ],
        }],
      }),
    );
  }, LOCAL_ANSWER);
  const turnReads: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/chat/sessions/chat-behind/turns")) turnReads.push(req.url());
  });

  await page.goto("/app/", { waitUntil: "load" });

  const asked = page.locator(`${SLOT} .pl-message--user`);
  await expect(asked).toHaveText([/First question/, /Second question from the other browser/, /Third question, stopped/]);
  const answers = page.locator(`${SLOT} .pl-message--assistant`);
  // The turn this browser already had keeps its own render; the server's copy is not laid over it.
  await expect(answers.nth(0)).toContainText(LOCAL_ANSWER);
  await expect(answers.nth(1)).toContainText("Second answer, from the other browser.");
  await expect(answers.nth(2).getByTestId("chat-stopped-mark")).toBeVisible();
  expect(turnReads.length).toBe(1);

  // Reconciled once: the next reload draws the same transcript, nothing doubled, and the
  // copy now answers for the server's count, so the turns are not read again.
  await page.reload({ waitUntil: "load" });
  await expect(asked).toHaveText([/First question/, /Second question from the other browser/, /Third question, stopped/]);
  await expect(answers).toHaveCount(3);
  await page.waitForTimeout(500);
  expect(turnReads.length).toBe(1);
});
