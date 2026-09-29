// A client-minted id for a transcript message or a pending attachment pill — unique enough
// for one browser (ms timestamp + random suffix). Shared by ChatSessionSlot and useAttachments.
export function messageId() {
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
