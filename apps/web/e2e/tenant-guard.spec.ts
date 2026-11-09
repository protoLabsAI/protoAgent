import { expect, test } from "@playwright/test";
import { persistedChat } from "./persistedChat";

// Tenant guard: the mock backend's instance_uid is "mock-uid-1". A stored uid from a
// DIFFERENT backend means another agent previously owned this origin — its persisted
// chat view is dropped (one reload) and the operator is told. Same uid = untouched.
//
// The seeded localStorage blob is migrated into IndexedDB at boot (epic #4981 S2), so the
// chat view under test is the IndexedDB copy: the switch must clear THAT (TenantGuard calls
// clearTranscriptDatabase), and the same uid must keep it.

test("a different backend uid clears the previous tenant's chat view", async ({ page }) => {
  await page.addInitScript(() => {
    // Seed ONCE: addInitScript re-runs on every navigation — including the guard's own
    // reload — and re-seeding the stale uid there would loop the guard forever.
    if (window.sessionStorage.getItem("e2e-tenant-seeded")) return;
    window.sessionStorage.setItem("e2e-tenant-seeded", "1");
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
  // The previous tenant's chat, migrated into IndexedDB before the guard ran, is gone from there too.
  const saved = await persistedChat(page);
  expect((saved?.sessions ?? []).map((s) => s.id)).not.toContain("c1");
  expect(JSON.stringify(saved ?? {})).not.toContain("Old tenant secrets");
  await expect(page.getByText("Old tenant secrets")).toHaveCount(0);
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
  // Kept: the chat survives, now in IndexedDB (the boot migration moved it off localStorage).
  await expect.poll(async () => ((await persistedChat(page))?.sessions ?? []).map((s) => s.id)).toContain("c2");
});
