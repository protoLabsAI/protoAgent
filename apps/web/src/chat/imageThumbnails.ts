import type { ChatMessage } from "../lib/types";

// Count the encoded URL, not just its binary payload, against the storage budget.
export const MAX_THUMB_LENGTH = 60_000;
export type ImageThumbnail = NonNullable<ChatMessage["attachments"]>[number];

export async function imageThumbnail(b64: string, mime: string): Promise<string> {
  const image = new Image();
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("Image preview unavailable"));
    image.src = `data:${mime};base64,${b64}`;
  });
  const scale = Math.min(1, 320 / Math.max(image.naturalWidth, image.naturalHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Image preview unavailable");
  for (let attempt = 0; attempt < 5; attempt++) {
    context.fillStyle = "white";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const thumb = canvas.toDataURL("image/jpeg", 0.75 - attempt * 0.1);
    if (thumb.startsWith("data:image/jpeg;base64,") && thumb.length < MAX_THUMB_LENGTH) return thumb;
    canvas.width = Math.max(1, Math.floor(canvas.width * 0.75));
    canvas.height = Math.max(1, Math.floor(canvas.height * 0.75));
  }
  throw new Error("Image preview exceeds storage budget");
}

export function validThumbnails(value: unknown): ImageThumbnail[] {
  if (!Array.isArray(value)) return [];
  return value.filter((a): a is ImageThumbnail => Boolean(a && typeof a.name === "string" &&
    typeof a.mime === "string" && typeof a.thumb === "string" && a.thumb.length < MAX_THUMB_LENGTH &&
    /^data:image\/(jpeg|webp);base64,[A-Za-z0-9+/]+=*$/.test(a.thumb)))
    .map(({ name, mime, thumb }) => ({ name, mime, thumb }));
}

/** Build a caption plus an "Attached: …" line for the supplied names (deduped, order
 *  preserved). Callers choose names for visible bubble text or durable fallback text. */
export function attachmentDisplay(text: string, names: string[]): string {
  const listed = [...new Set(names.filter(Boolean))].join(", ");
  return listed ? [text, `Attached: ${listed}`].filter(Boolean).join("\n\n") : text;
}

/** Resolve one composer send with attachments into what the turn needs: the inline image
 *  `previews` for the bubble, the local `bubble` text, and the `durable` display persisted
 *  server-side (ADR 0104). A previewable image is SHOWN inline, so it's named only in
 *  `durable` — a chat rebuilt in another browser (or after a cleared cache / dropped preview)
 *  has no cached thumbnail, so without the name it would come back blank. An image whose
 *  preview failed, and every piped file, are named in BOTH so the local bubble is never
 *  blank and an image-only turn never saves as empty text (which would lose the turn, and
 *  its title, on rebuild). */
export function sentAttachments(
  text: string,
  images: { name: string; mime?: string; thumb?: string }[],
  pipedNames: string[],
): { previews: ImageThumbnail[]; bubble: string; durable: string } {
  const previews = images.flatMap((a) =>
    a.thumb ? [{ name: a.name, mime: a.mime || "image/png", thumb: a.thumb }] : [],
  );
  const unpreviewable = images.filter((a) => !a.thumb).map((a) => a.name);
  return {
    previews,
    bubble: attachmentDisplay(text, [...pipedNames, ...unpreviewable]),
    durable: attachmentDisplay(text, [...pipedNames, ...images.map((a) => a.name)]),
  };
}
