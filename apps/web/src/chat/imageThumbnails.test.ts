import { afterEach, expect, it, vi } from "vitest";
import { attachmentDisplay, imageThumbnail, MAX_THUMB_LENGTH, sentAttachments, validThumbnails } from "./imageThumbnails";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each([[1600, 800, 320, 160], [600, 1200, 160, 320], [80, 40, 80, 40]])(
  "downscales %sx%s without retaining original bytes", async (width, height, expectedWidth, expectedHeight) => {
    vi.stubGlobal("Image", class {
      naturalWidth = width;
      naturalHeight = height;
      onload = () => {};
      set src(_value: string) { queueMicrotask(() => this.onload()); }
    });
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage, fillRect: vi.fn() } as never);
    const encode = vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue("data:image/jpeg;base64,YQ==");
    const thumb = await imageThumbnail("ORIGINAL", "image/png");
    expect(drawImage.mock.calls[0].slice(1)).toEqual([0, 0, expectedWidth, expectedHeight]);
    expect(encode).toHaveBeenCalledWith("image/jpeg", 0.75);
    expect(thumb).not.toContain("ORIGINAL");
    expect(thumb.length).toBeLessThan(MAX_THUMB_LENGTH);
    expect(validThumbnails([{ name: "photo", mime: "image/png", thumb, b64: "ORIGINAL" }]))
      .toEqual([{ name: "photo", mime: "image/png", thumb }]);
  },
);

it("appends a deduped Attached line only when names are present", () => {
  expect(attachmentDisplay("Look", ["notes.txt"])).toBe("Look\n\nAttached: notes.txt");
  expect(attachmentDisplay("", [])).toBe("");
  expect(attachmentDisplay("", ["notes.txt"])).toBe("Attached: notes.txt");
  expect(attachmentDisplay("Look", ["a.txt", "a.txt", "b.txt"])).toBe("Look\n\nAttached: a.txt, b.txt");
});

it("shows previewable images inline but names piped files, preview-less images, and every image durably", () => {
  const withThumb = { name: "photo.png", mime: "image/png", thumb: "data:image/jpeg;base64,YQ==" };
  const noThumb = { name: "broken.gif", mime: "image/gif" }; // thumbnail generation failed

  // Caption + a previewable image + a piped file: the image is SHOWN (not named in the
  // bubble) but the durable display, which rebuilds with no cached thumbnail, names it too.
  const r1 = sentAttachments("Look", [withThumb], ["notes.txt"]);
  expect(r1.previews).toEqual([withThumb]);
  expect(r1.bubble).toBe("Look\n\nAttached: notes.txt");
  expect(r1.durable).toBe("Look\n\nAttached: notes.txt, photo.png");

  // Image-only send with a working preview: blank caption, no "Attached:" line locally, but
  // the durable display still names the image so the turn (and its title) survive a rebuild.
  const r2 = sentAttachments("", [withThumb], []);
  expect(r2.previews).toEqual([withThumb]);
  expect(r2.bubble).toBe("");
  expect(r2.durable).toBe("Attached: photo.png");

  // Thumbnail FAILED: no preview, so the image falls back to the "Attached:" line in BOTH
  // the bubble and the durable display — an image-only send is never a blank message.
  const r3 = sentAttachments("", [noThumb], []);
  expect(r3.previews).toEqual([]);
  expect(r3.bubble).toBe("Attached: broken.gif");
  expect(r3.durable).toBe("Attached: broken.gif");
});

it("rejects full-size, malformed, and non-image previews", () => {
  expect(validThumbnails([
    { name: "huge", mime: "image/jpeg", thumb: "data:image/jpeg;base64," + "A".repeat(MAX_THUMB_LENGTH) },
    { name: "remote", mime: "image/jpeg", thumb: "https://example.com/image" },
    null,
  ])).toEqual([]);
});

it("reduces a noisy preview again until the encoded URL fits the budget", async () => {
  vi.stubGlobal("Image", class {
    naturalWidth = 1600;
    naturalHeight = 800;
    onload = () => {};
    set src(_value: string) { queueMicrotask(() => this.onload()); }
  });
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage, fillRect: vi.fn() } as never);
  vi.spyOn(HTMLCanvasElement.prototype, "toDataURL")
    .mockReturnValueOnce("data:image/jpeg;base64," + "A".repeat(MAX_THUMB_LENGTH))
    .mockReturnValue("data:image/jpeg;base64,YQ==");
  await expect(imageThumbnail("ORIGINAL", "image/png")).resolves.toBe("data:image/jpeg;base64,YQ==");
  expect(drawImage.mock.calls[1].slice(1)).toEqual([0, 0, 240, 120]);
});
