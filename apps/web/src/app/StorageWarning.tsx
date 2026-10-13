import { useSyncExternalStore } from "react";
import { Banner } from "@protolabsai/ui/primitives";
import { storagePressure, subscribeStoragePressure } from "../lib/storage";

/** A failed cache write must remain visible while the console continues in memory. */
export function StorageWarning() {
  const pressure = useSyncExternalStore(subscribeStoragePressure, storagePressure, storagePressure);
  if (pressure.state !== "failing") return null;
  return (
    <Banner tone="warning" title="Browser storage is full" className="shell-warning-banner">
      Recent changes may not survive a reload. Export conversations you need to keep, then delete old chats to free space.
    </Banner>
  );
}
