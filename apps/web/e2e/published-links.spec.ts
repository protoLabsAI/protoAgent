import { expect, test } from "@playwright/test";

// Settings ▸ Publish (#2179 P2, #2684): the schema-driven publish.* fields (endpoint
// URLs) render with the Published Links card beneath them — same footer-seam pattern
// as Settings ▸ Secrets. Behind chat.publish (ADR 0068, off by default), so every test
// forces it on via the shareable ?flag: query override, same as commands.spec.ts /
// publish-dialog.spec.ts.

async function openPublishSettings(page: import("@playwright/test").Page) {
  await page.goto("/app/?flag:chat.publish=on", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Publish", exact: true }).click();
}

test("Publish settings shows the schema fields and an empty published-links state", async ({ page }) => {
  await page.route("**/api/chat/publish/links", (route) => route.fulfill({ json: { links: [] } }));
  await openPublishSettings(page);

  // Schema-driven fields (generic form).
  await expect(page.getByText("Hosted publish endpoint URL")).toBeVisible();
  await expect(page.getByText("Revoke endpoint URL")).toBeVisible();

  // Footer card: empty state, not a blank gap.
  const card = page.getByTestId("published-links");
  await expect(card).toBeVisible();
  await expect(card.getByText("Nothing published yet")).toBeVisible();
});

test("lists a published link and revoking it removes the revoke action", async ({ page }) => {
  // Stateful mock: the panel invalidates + refetches the list after a successful revoke
  // (react-query), so the GET must reflect the change on the SECOND call, same as a real
  // server would — a mock that always answers identically can't tell "revoked" apart
  // from "still live".
  let revoked = false;
  await page.route("**/api/chat/publish/links", (route) =>
    route.fulfill({
      json: {
        links: [
          {
            id: "abc123",
            thread_id: "t1",
            title: "Merck sync notes",
            public_url: "https://protolabs.studio/c/abc123",
            published_at: 1755000000,
            expires_at: null,
            revoked_at: revoked ? 1755000100 : null,
          },
        ],
      },
    }),
  );
  await page.route("**/api/chat/publish/links/abc123/revoke", (route) => {
    revoked = true;
    return route.fulfill({ json: { ok: true } });
  });
  await openPublishSettings(page);

  const card = page.getByTestId("published-links");
  await expect(card.getByText("Merck sync notes")).toBeVisible();
  await expect(card.getByRole("link", { name: "https://protolabs.studio/c/abc123" })).toBeVisible();

  await card.getByRole("button", { name: /revoke/i }).click();
  await expect(page.locator(".pl-toast", { hasText: "Link revoked" })).toBeVisible();
  await expect(card.getByText("revoked")).toBeVisible();
  await expect(card.getByRole("button", { name: /revoke/i })).toHaveCount(0);
});

test("a not-configured revoke failure toasts and leaves the link listed as live", async ({ page }) => {
  await page.route("**/api/chat/publish/links", (route) =>
    route.fulfill({
      json: {
        links: [
          {
            id: "abc123",
            thread_id: "t1",
            title: "Merck sync notes",
            public_url: "https://protolabs.studio/c/abc123",
            published_at: 1755000000,
            expires_at: null,
            revoked_at: null,
          },
        ],
      },
    }),
  );
  await page.route("**/api/chat/publish/links/abc123/revoke", (route) =>
    route.fulfill({ json: { ok: false, reason: "not_configured" } }),
  );
  await openPublishSettings(page);

  const card = page.getByTestId("published-links");
  await card.getByRole("button", { name: /revoke/i }).click();

  await expect(page.locator(".pl-toast", { hasText: /still live/i })).toBeVisible();
  // Nothing local changed — the link still reads as live, revoke action still offered.
  await expect(card.getByRole("link", { name: "https://protolabs.studio/c/abc123" })).toBeVisible();
  await expect(card.getByRole("button", { name: /revoke/i })).toBeVisible();
});
