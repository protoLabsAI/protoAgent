import { expect, test } from "@playwright/test";

// Images always attach natively as multimodal parts (#1969): a vision model sees
// them, and on a text-only model the server bridges them into the media store so
// image tools can act on them by id — the old #1374 hard error is gone. A configured
// describe model (#1381) additionally contributes a textual description.

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

// Force the runtime model's vision / image_describe capabilities for a single test.
async function forceModel(page, { vision, image_describe }) {
  await page.route("**/api/runtime/status", async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    if (json?.model) {
      json.model.vision = vision;
      json.model.image_describe = image_describe;
    }
    await route.fulfill({ json });
  });
}

test("a text-only model with NO describe model still attaches an image natively (#1969)", async ({ page }) => {
  await forceModel(page, { vision: false, image_describe: false });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  const slot = page.locator(".chat-session-slot:not([hidden])");

  await slot.locator('input[type="file"]').setInputFiles({ name: "Screenshot.png", mimeType: "image/png", buffer: PNG_1X1 });

  // No error and no pipeline round-trip: the image rides the turn natively; the
  // server persists it to the media store so tools can reference it by id.
  const chips = slot.locator(".pl-prompt__attachments");
  await expect(chips).toContainText("Screenshot.png");
  await expect(chips).not.toContainText("uploading");
  await expect(page.getByText(/vision-capable model/i)).toHaveCount(0);
});

test("a text-only model WITH a describe model attaches the image via the pipeline (#1381)", async ({ page }) => {
  await forceModel(page, { vision: false, image_describe: true });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  const slot = page.locator(".chat-session-slot:not([hidden])");

  await slot.locator('input[type="file"]').setInputFiles({ name: "Screenshot.png", mimeType: "image/png", buffer: PNG_1X1 });

  // No error: the image routes to /attach (the server describes it) and the chip settles ready.
  const chips = slot.locator(".pl-prompt__attachments");
  await expect(chips).toContainText("Screenshot.png");
  await expect(chips).not.toContainText("uploading");
  await expect(page.getByText(/vision-capable model/i)).toHaveCount(0);
});
