// Origins, besides this page's and the API base, that the FOCUSED agent serves its own media
// from: its public URL (`A2A_PUBLIC_URL`), which signed media links in a reply are minted
// against. Learned from that agent's runtime status; read by the chat's remote-image check.
let publicOrigin = "";

/** Record the focused agent's public URL (as its runtime status reports it). */
export function noteAgentPublicUrl(url: unknown): void {
  try {
    publicOrigin = typeof url === "string" && url ? new URL(url).origin : "";
  } catch {
    publicOrigin = "";
  }
}

export function agentPublicOrigin(): string {
  return publicOrigin;
}
