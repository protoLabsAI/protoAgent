import type { SessionStatus } from "./chat-store";

// The composer's placeholder line. Short hints only (#1699) — key/command discoverability
// lives in /help now, not in a placeholder wall of text competing with the message being
// written. Three states:
//   idle                      → "Message <agent name>…" — the agent's display name (same
//                               source as the header and PaletteChat: brandName(identity.name)),
//                               so a renamed agent no longer reads "Message protoAgent…". Falls
//                               back to "Message the agent…" when no name is known.
//   streaming, nothing queued → "Steer the agent…" (an e2e anchor — chat-steer-cancel.spec.ts
//                               grabs it BEFORE queueing, so the anchor survives the swap)
//   streaming, steer queued   → a ↑-recall hint (#2837): queueSteer pushes the sent text
//                               into input history, so ↑ in the empty composer recalls the
//                               queued message for editing — but nothing in the UI said so.
export function composerPlaceholder(
  status: SessionStatus,
  queuedSteers: number,
  agentName?: string | null,
): string {
  if (status !== "streaming") {
    const name = (agentName ?? "").trim();
    return name ? `Message ${name}…` : "Message the agent…";
  }
  return queuedSteers > 0 ? "Press ↑ to edit queued message" : "Steer the agent…";
}
