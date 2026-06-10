import { expect, test } from "@playwright/test";

// Tenant guard: the mock backend's instance_uid is "mock-uid-1". A stored uid from a
// DIFFERENT backend means another agent previously owned this origin — its persisted
// chat view is dropped (one reload) and the operator is told. Same uid = untouched.

test("a different backend uid clears the previous tenant's chat view", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.tenant.uid", "previous-agent-uid");
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({ version: 1, currentSessionId: null, sessions: [{ id: "c1", title: "Old tenant secrets", messages: [], createdAt: 1, updatedAt: 1 }] }),
    );
  });
  await page.goto("/app/", { waitUntil: "load" });

  // The guard clears + reloads, then toasts on the fresh page.
  await expect(page.getByText("Different agent on this address")).toBeVisible({ timeout: 10_000 });
  const state = await page.evaluate(() => ({
    chats: window.localStorage.getItem("protoagent.chat.sessions"),
    uid: window.localStorage.getItem("protoagent.tenant.uid"),
  }));
  expect(state.chats).toBeNull();
  expect(state.uid).toBe("mock-uid-1");
});

test("the same backend uid keeps the chat view (restart/upgrade case)", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.tenant.uid", "mock-uid-1");
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({ version: 1, currentSessionId: null, sessions: [{ id: "c2", title: "Kept", messages: [], createdAt: 1, updatedAt: 1 }] }),
    );
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.getByText("Different agent on this address")).toHaveCount(0);
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.chat.sessions"))).not.toBeNull();
});
