import { expect, test } from "@playwright/test";

// Composer paste behaviour (bd-873 / bd-2pp): a large text paste becomes a
// removable attachment pill instead of flooding the field, and a pasted image
// (delivered via clipboard items[]) becomes an attachment.

const FIELD = ".chat-session-slot:not([hidden]) .pl-prompt__field";

// Dispatch a synthetic paste carrying text on the composer textarea.
async function pasteText(page, text: string) {
  await page.evaluate(
    ({ sel, text }) => {
      const ta = document.querySelector(sel) as HTMLTextAreaElement;
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      ta.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    },
    { sel: FIELD, text },
  );
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("a large text paste becomes a removable attachment, not field text", async ({ page }) => {
  const slot = page.locator(".chat-session-slot:not([hidden])");
  await pasteText(page, "X".repeat(2000));

  const chips = slot.locator(".pl-prompt__attachments");
  await expect(chips).toContainText("Pasted text.txt");
  await expect(chips).not.toContainText("uploading");
  await expect(page.locator(FIELD)).toHaveValue(""); // not dumped into the input

  // Removable like any attachment.
  await chips.getByRole("button", { name: /remove/i }).first().click();
  await expect(chips).toHaveCount(0);
});

test("a short text paste falls through to the field (no attachment)", async ({ page }) => {
  const slot = page.locator(".chat-session-slot:not([hidden])");
  await pasteText(page, "just a short note");
  await expect(slot.locator(".pl-prompt__attachments")).toHaveCount(0);
});

test("a pasted image (clipboard items) becomes an attachment", async ({ page }) => {
  const slot = page.locator(".chat-session-slot:not([hidden])");
  await page.evaluate((sel) => {
    const ta = document.querySelector(sel) as HTMLTextAreaElement;
    const b64 =
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const file = new File([bytes], "shot.png", { type: "image/png" });
    const dt = new DataTransfer();
    dt.items.add(file);
    ta.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  }, FIELD);

  await expect(slot.locator(".pl-prompt__attachments")).toContainText("shot.png");
});
