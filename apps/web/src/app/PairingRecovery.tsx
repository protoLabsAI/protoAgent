import { Button } from "@protolabsai/ui/primitives";
import { useState, type ReactNode } from "react";
import "./app-crash.css";

/** Pairing happens before mount. Show its failure before the auth dialog can obscure it. */
export function PairingRecovery({ error, children }: { error: string | null; children: ReactNode }) {
  const [dismissed, setDismissed] = useState(false);
  if (!error || dismissed) return children;
  return (
    <main className="app-crash" role="alert">
      <h1>Device pairing failed</h1>
      <p className="app-crash__msg">{error}</p>
      <p className="app-crash__hint">
        If browser storage is full, export conversations you need to keep and delete old chats.
        If storage is blocked, enable it for this site. Then request a new pairing code from
        Settings → Devices on the original device. The previous code may already be spent.
      </p>
      <Button onClick={() => setDismissed(true)}>Continue to console</Button>
    </main>
  );
}
