// Copy text to the system clipboard and report whether it worked.
//
// `navigator.clipboard` is missing outside a secure context (plain-http LAN access to
// the console), and `writeText` rejects when the document isn't focused or the
// permission is denied. A caller that fires `writeText` and shows "Copied" right away
// claims success it never had, and one that doesn't catch the rejection leaks an
// unhandled promise. This never rejects: it resolves true only once the write landed.

export async function copyText(text: string): Promise<boolean> {
  const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
  if (!clipboard || typeof clipboard.writeText !== "function") return false;
  try {
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
