import { expect, test } from "@playwright/test";

test("sent images render bounded previews that survive reload", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(composer).toBeVisible();
  const original = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 1600;
    canvas.height = 800;
    canvas.getContext("2d")!.fillRect(0, 0, 1600, 800);
    return canvas.toDataURL("image/png").split(",")[1];
  });
  const slot = page.locator(".chat-session-slot:not([hidden])");
  await slot.locator('input[type="file"]').setInputFiles({
    name: "photo.png", mimeType: "image/png", buffer: Buffer.from(original, "base64"),
  });
  await expect(slot.locator(".pl-prompt__attachments")).toContainText("photo.png");
  await expect(slot.locator(".pl-prompt__attachments")).not.toContainText("uploading");
  await composer.fill("Describe this photo");
  await composer.press("Enter");
  const thumbnail = slot.getByRole("button", { name: "Preview photo.png" });
  await expect(thumbnail).toBeVisible();
  await expect(slot).not.toContainText("Attached: photo.png");
  expect(await thumbnail.locator("img").evaluate((img: HTMLImageElement) => [img.naturalWidth, img.naturalHeight]))
    .toEqual([320, 160]);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("protoagent.chat.sessions"))).toContain("data:image/jpeg;");
  const saved = await page.evaluate(() => localStorage.getItem("protoagent.chat.sessions")!);
  expect(saved).not.toContain(original);
  await page.reload();
  await expect(thumbnail).toBeVisible();
  await thumbnail.click();
  await expect(page.getByRole("dialog").getByRole("img", { name: "photo.png" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
});
