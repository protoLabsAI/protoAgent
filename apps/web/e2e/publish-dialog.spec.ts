import { expect, test } from "@playwright/test";

// The pre-publish review dialog (#2179 P2, #2682) — end-to-end wiring check: firing
// /publish opens PublishDialog (subscribed via publishDialogStore), it fetches and
// renders the preview, and confirming calls the publish endpoint and posts a status note.
// Network mocked throughout — this is about the console's OWN wiring, not the server's
// bundle-building logic (covered server-side by tests/test_publish_session.py /
// tests/test_chat_bundle.py) or the note-posting logic (covered by publishChat.test.ts).

const PREVIEW_BODY = {
  found: true,
  manifest: {
    bundle_version: 1,
    kind: "chat-bundle",
    exported_at: "2026-08-13 09:00 UTC",
    thread_id: "t1",
    title: "Test chat",
    messages: [{ role: "user", parts: [{ kind: "text", text: "hello from the preview" }] }],
  },
  message_count: 1,
  redactions: ["openai-key"],
  reason: "ok",
  message: "1 message(s) ready to review.",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/chat/sessions/*/publish/preview*", (route) => route.fulfill({ json: PREVIEW_BODY }));
  await page.goto("/app/?flag:chat.publish=on", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

async function openViaSlashCommand(page: import("@playwright/test").Page) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/publish");
  await composer.press("Enter");
}

test("/publish opens the review dialog with the preview content and redaction summary", async ({ page }) => {
  await openViaSlashCommand(page);

  await expect(page.getByText("Publish this chat?")).toBeVisible();
  await expect(page.getByText("hello from the preview")).toBeVisible();
  await expect(page.getByText(/1 secret pattern\(s\) redacted/)).toBeVisible();
  await expect(page.getByText(/public, unauthenticated link/)).toBeVisible();
});

test("Cancel closes the dialog and publishes nothing", async ({ page }) => {
  let publishCalled = false;
  await page.route("**/api/chat/sessions/*/publish", (route) => {
    publishCalled = true;
    return route.fulfill({ json: { published: false, message: "should not be called" } });
  });

  await openViaSlashCommand(page);
  await expect(page.getByText("Publish this chat?")).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();

  await expect(page.getByText("Publish this chat?")).toBeHidden();
  expect(publishCalled).toBe(false);
});

test("confirming publishes and posts a success note with the link", async ({ page }) => {
  await page.route("**/api/chat/sessions/*/publish", (route) =>
    route.fulfill({
      json: {
        published: true,
        public_url: "https://protolabs.studio/c/e2e-test",
        revoke_token: "rvk_e2e",
        expires_at: null,
        redactions: ["openai-key"],
        artifact_notes: [],
        message: "Published",
      },
    }),
  );

  await openViaSlashCommand(page);
  await expect(page.getByText("Publish this chat?")).toBeVisible();
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  await expect(page.getByText("Publish this chat?")).toBeHidden();
  await expect(page.getByText(/Published/)).toBeVisible();
  await expect(page.getByText(/protolabs\.studio\/c\/e2e-test/)).toBeVisible();
});

test("not-configured surfaces as a status note, not a crash", async ({ page }) => {
  await page.route("**/api/chat/sessions/*/publish", (route) =>
    route.fulfill({
      json: { published: false, reason: "not_configured", message: "Hosted publishing isn't configured on this instance yet." },
    }),
  );

  await openViaSlashCommand(page);
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  await expect(page.getByText("Hosted publishing isn't configured on this instance yet.")).toBeVisible();
});
