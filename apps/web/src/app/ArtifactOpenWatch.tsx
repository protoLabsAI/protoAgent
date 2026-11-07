import { useEffect } from "react";
import { chatStore } from "../chat/chat-store";
import { apiUrl } from "../lib/api";
import { onServerEvent } from "../lib/events";

// Only the currently attended chat may request a tab. Never replay these effects on mount.
export function openArtifactEvent(data: Record<string, unknown>, session: string | null) {
  if (!session || data.session_id !== session || typeof data.path !== "string") return;
  // This event is intentionally not a general-purpose URL opener.
  if (!/^\/plugins\/artifact\/media\/artifact\/[A-Za-z0-9_-]+\/blob\?version=[1-9]\d*&exp=[1-9]\d*&t=[a-f0-9]{64}$/.test(data.path)) return;
  window.open(apiUrl(data.path), "_blank", "noopener,noreferrer");
}

export function ArtifactOpenWatch() {
  useEffect(() => onServerEvent("artifact.open_url", (data) => {
    openArtifactEvent(data, chatStore.getSnapshot().currentSessionId);
  }), []);
  return null;
}
