import { expect, test } from "@playwright/test";

// Console resilience (#872): corrupt persisted chat state must never white-screen
// the app. loadPersisted's sanitize pass drops invalid sessions (keeping valid
// ones) and starts a fresh session when nothing survives; the root error boundary
// is the backstop for anything else.

test("a corrupt chat-sessions blob boots a fresh chat, not a white screen", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.chat.sessions", "{definitely not json");
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("invalid session members are dropped while valid sessions survive", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({
        version: 1,
        currentSessionId: "good",
        sessions: [
          {
            id: "good",
            title: "Survivor chat",
            messages: [{ role: "user", content: "hello" }],
            createdAt: 1,
            updatedAt: 1,
          },
          { id: "shapeless" }, // missing every other field — dropped
          null, // not even an object — dropped
        ],
      }),
    );
  });
  await page.goto("/app/", { waitUntil: "load" });
  // The valid session survived the sanitize pass…
  await expect(page.getByRole("tab", { name: "Survivor chat" })).toBeVisible();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  // …and the dropped members are gone from storage after the next persist.
});

test("currentSessionId pointing at a dropped session re-points at a survivor", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({
        version: 1,
        currentSessionId: "the-corrupt-one",
        sessions: [
          { id: "the-corrupt-one", title: "Broken", messages: [{ role: "user" }], createdAt: 1, updatedAt: 1 },
          {
            id: "ok",
            title: "Still here",
            messages: [{ role: "user", content: "hi" }],
            createdAt: 1,
            updatedAt: 1,
          },
        ],
      }),
    );
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByRole("tab", { name: "Still here" })).toBeVisible();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});
