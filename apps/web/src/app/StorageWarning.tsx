import { useSyncExternalStore } from "react";
import { Banner } from "@protolabsai/ui/primitives";
import { storagePressure, subscribeStoragePressure } from "../lib/storage";
import { subscribeTranscriptSaveFailing, transcriptSaveFailing, transcriptStoreIfOpen } from "../chat/chat-store";

/** A failed save must stay visible while the console keeps working in memory: localStorage full
 *  (ADR 0114 D1), or a chat save to IndexedDB failing (epic #4981). Same words as Settings →
 *  Storage's full state (docs/design/settings-storage.md §3.1), which has Free up space. */
export function StorageWarning() {
  const pressure = useSyncExternalStore(subscribeStoragePressure, storagePressure, storagePressure);
  const chatsFailing = useSyncExternalStore(subscribeTranscriptSaveFailing, transcriptSaveFailing, transcriptSaveFailing);
  const localFull = pressure.state === "failing";
  // Chats aren't saving when an IndexedDB save fails, or when localStorage is full on the
  // localStorage path. A full localStorage while chats live in IndexedDB only touches layout
  // and preferences, and saying "messages aren't being saved" then would be false.
  const chatsOnLocal = !transcriptStoreIfOpen();
  if (chatsFailing || (localFull && chatsOnLocal)) {
    return (
      <Banner tone="warning" title="New messages aren't being saved in this browser" className="shell-warning-banner">
        Chats keep working, and the server keeps the last 24 hours of each one. Free up space in Settings → Storage to start saving again.
      </Banner>
    );
  }
  if (!localFull) return null;
  return (
    <Banner tone="warning" title="Some console settings aren't being saved in this browser" className="shell-warning-banner">
      Your chats are saving. Layout and preferences changes may not survive a reload. Settings → Storage shows what's using the space.
    </Banner>
  );
}
