import { afterEach, expect, it, vi } from "vitest";
import { attachmentDisplay, imageThumbnail, MAX_THUMB_LENGTH, validThumbnails } from "./imageThumbnails";

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

it("lists only piped files, including when an image also has description context", () => {
  expect(attachmentDisplay("Look", [{ name: "photo.png", native: true }, { name: "notes.txt" }]))
    .toBe("Look\n\nAttached: notes.txt");
  expect(attachmentDisplay("", [{ name: "photo.png", native: true }])).toBe("");
  expect(attachmentDisplay("", [{ name: "notes.txt" }])).toBe("Attached: notes.txt");
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
