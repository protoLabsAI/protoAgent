import { useSyncExternalStore } from "react";
import { Banner } from "@protolabsai/ui/primitives";
import { storagePressure, subscribeStoragePressure } from "../lib/storage";
import { subscribeTranscriptSaveFailing, transcriptSaveFailing } from "../chat/chat-store";

/** A failed save must stay visible while the console keeps working in memory: localStorage full
 *  (ADR 0114 D1), or a chat save to IndexedDB failing (epic #4981). Same words as Settings →
 *  Storage's full state (docs/design/settings-storage.md §3.1), which has Free up space. */
export function StorageWarning() {
  const pressure = useSyncExternalStore(subscribeStoragePressure, storagePressure, storagePressure);
  const chatsFailing = useSyncExternalStore(subscribeTranscriptSaveFailing, transcriptSaveFailing, transcriptSaveFailing);
  if (pressure.state !== "failing" && !chatsFailing) return null;
  return (
    <Banner tone="warning" title="New messages aren't being saved in this browser" className="shell-warning-banner">
      Chats keep working, and the server keeps the last 24 hours of each one. Free up space in Settings → Storage to start saving again.
    </Banner>
  );
}
