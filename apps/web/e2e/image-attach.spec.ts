import { expect, test } from "@playwright/test";

// #1374: dropping/attaching an image (e.g. a macOS screenshot — PNG) only works on a
// vision-capable model. On a TEXT-ONLY model the file pipeline can't read images (no OCR),
// so the composer short-circuits with a clear, actionable error instead of the cryptic
// "unsupported file type" the extractor returns.

const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

test("a vision model attaches an image inline (no error)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  const slot = page.locator(".chat-session-slot:not([hidden])");

  await slot.locator('input[type="file"]').setInputFiles({
    name: "Screenshot.png",
    mimeType: "image/png",
    buffer: PNG_1X1,
  });

  const chips = slot.locator(".pl-prompt__attachments");
  await expect(chips).toContainText("Screenshot.png");
  await expect(chips).not.toContainText("uploading"); // settled (native inline)
  await expect(chips).not.toContainText(/vision-capable model/i); // no error
});

test("a text-only model rejects an image with a clear 'needs a vision model' error", async ({ page }) => {
  // Force the model non-vision (like protolabs/reasoning → deepseek) for this test only.
  await page.route("**/api/runtime/status", async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    if (json?.model) json.model.vision = false;
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  const slot = page.locator(".chat-session-slot:not([hidden])");

  await slot.locator('input[type="file"]').setInputFiles({
    name: "Screenshot.png",
    mimeType: "image/png",
    buffer: PNG_1X1,
  });

  // The chip appears (in an error state), and the clear, actionable message surfaces in the
  // alert banner — NOT a cryptic "unsupported file type" from the extractor (never called).
  await expect(slot.locator(".pl-prompt__attachments")).toContainText("Screenshot.png");
  await expect(page.getByText(/vision-capable model/i)).toBeVisible();
});
