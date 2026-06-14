// Clipboard / drag-drop → attachment helpers for the chat composer.
//
// Two behaviours sit on top of these:
//  • pasted IMAGES (screenshots) and files become attachments — see
//    `filesFromTransfer` (catches images a browser exposes only via `items`);
//  • pasted TEXT over a threshold becomes a removable attachment pill instead
//    of flooding the input — see `isLargePaste`.

// Pasted text longer than this (characters OR lines) is attached rather than
// inserted into the field. Tuned so a normal message stays inline but a pasted
// log / article / code block becomes an attachment.
export const LARGE_PASTE_CHARS = 1500;
export const LARGE_PASTE_LINES = 20;

export function isLargePaste(text: string): boolean {
  if (!text) return false;
  return text.length > LARGE_PASTE_CHARS || text.split("\n").length > LARGE_PASTE_LINES;
}

// Give an unnamed clipboard blob (e.g. a pasted screenshot) a sensible filename
// so the attachment pill has a label and the backend gets an extension.
export function namedFile(f: File): File {
  if (f.name) return f;
  const isImg = f.type.startsWith("image/");
  const ext = (f.type.split("/")[1] || "").replace(/[^a-z0-9]/gi, "") || (isImg ? "png" : "bin");
  return new File([f], `pasted-${isImg ? "image" : "file"}.${ext}`, { type: f.type });
}

// Collect Files from a clipboard or drag payload. Prefer `items[].getAsFile()`
// so clipboard IMAGES that a browser surfaces only via `items` (not `.files`)
// are caught; fall back to `.files`. Unnamed blobs are given a name.
export function filesFromTransfer(dt: DataTransfer | null | undefined): File[] {
  if (!dt) return [];
  const fromItems: File[] = [];
  for (const item of Array.from(dt.items ?? [])) {
    if (item.kind === "file") {
      const f = item.getAsFile();
      if (f) fromItems.push(namedFile(f));
    }
  }
  if (fromItems.length) return fromItems;
  return Array.from(dt.files ?? []).map(namedFile);
}

// The synthetic file a large text paste is wrapped in (routed through the same
// attach pipeline as a dropped .txt, so it gets tiered inline/indexed).
export function pastedTextFile(text: string): File {
  return new File([text], "Pasted text.txt", { type: "text/plain" });
}
