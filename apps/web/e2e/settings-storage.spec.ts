import { expect, test, type Page } from "@playwright/test";

import { persistedChat } from "./persistedChat";

// Settings ▸ Storage (docs/design/settings-storage.md, #5015): the page lists what this browser
// keeps per agent, and Clear removes one agent's chats from this browser's IndexedDB, leaving
// every other agent's.

/** Save chats for `slug` straight into the console's transcript database, the shape the store
 *  writes (chat/transcriptStore.ts): a session record whose tail holds the messages. */
async function seedChats(page: Page, slug: string, ids: string[]) {
  await page.evaluate(
    async ([slug, ids]) => {
      const db = await new Promise<IDBDatabase>((ok, no) => {
        const r = indexedDB.open("protoagent-transcripts");
        r.onsuccess = () => ok(r.result);
        r.onerror = () => no(r.error);
      });
      const tx = db.transaction("sessions", "readwrite");
      for (const id of ids) {
        tx.objectStore("sessions").put({
          slug,
          id,
          updatedAt: Date.now(),
          session: { title: id, createdAt: 1 },
          chunks: [],
          tail: [
            { id: `${id}-u`, role: "user", content: "hello", status: "done" },
            { id: `${id}-a`, role: "assistant", content: "x".repeat(4000), status: "done" },
          ],
        });
      }
      await new Promise<void>((ok, no) => {
        tx.oncomplete = () => ok();
        tx.onerror = () => no(tx.error);
      });
      db.close();
    },
    [slug, ids] as const,
  );
}

test("Settings ▸ Storage clears one agent's chats from this browser", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".app-shell-main")).toBeVisible();
  // The console has opened (and so created) its transcript database by now.
  await expect.poll(() => page.evaluate(async () => (await indexedDB.databases()).some((d) => d.name === "protoagent-transcripts"))).toBe(true);
  await seedChats(page, "ava", ["ava-1", "ava-2"]);
  await seedChats(page, "ghost", ["ghost-1"]);

  await page.keyboard.press("ControlOrMeta+Comma");
  await page.locator(".settings-overlay").getByText("Storage", { exact: true }).click();
  await expect(page.getByText("Chats are saving in this browser.")).toBeVisible();

  const ava = page.locator(".storage-row", { hasText: "ava" });
  await expect(ava).toContainText("2 chats");
  // An agent that left the fleet keeps its name, with a neutral badge.
  await expect(page.locator(".storage-row", { hasText: "ghost" })).toContainText("not in fleet");

  await page.getByRole("button", { name: "Clear ava's chats" }).click();
  const dialog = page.getByRole("dialog", { name: "Clear ava's chats from this browser?" });
  await expect(dialog).toContainText("Removes 2 saved chats");
  await dialog.getByRole("button", { name: "Clear chats" }).click();

  await expect(page.getByText(/Cleared ava's chats from this browser\. .* freed\./)).toBeVisible();
  await expect(page.locator(".storage-row", { hasText: "ava" })).toHaveCount(0);
  expect(await persistedChat(page, "ava")).toBeNull();
  expect((await persistedChat(page, "ghost"))?.sessions.map((s) => s.id)).toEqual(["ghost-1"]);
});
