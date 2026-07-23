// Trigger a client-side file download from in-memory text — a plain <a download>.click()
// on a Blob URL. Used by the chat-export gesture (#2158) to save a thread's Markdown; kept
// generic (mime defaults to Markdown) so other text exports can reuse it. Best-effort: on a
// surface that blocks programmatic downloads it no-throws (the caller still has the data).
export function downloadTextFile(filename: string, text: string, mime = "text/markdown"): void {
  try {
    const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }));
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke after the click has been dispatched so the download isn't cancelled.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch {
    /* download blocked (sandbox / policy) — caller keeps the payload */
  }
}

// A safe, readable file stem from a chat title (or a fallback). Collapses whitespace and
// drops path-hostile characters so the browser doesn't reject or rewrite the name.
export function safeFilename(base: string, fallback = "chat"): string {
  const cleaned = (base || "")
    .replace(/[/\\:*?"<>|\r\n\t]+/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[.\-\s]+|[.\-\s]+$/g, "");
  return cleaned || fallback;
}
