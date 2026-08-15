import { Checkbox } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { useState } from "react";

// The one-time "this runs code" consent (ADR 0071 D3, #2721). The install route
// answers `needs_ack` for a source that is neither official nor previously acked;
// this dialog is the ack. Confirm persists the EXACT repo into
// `plugins.sources.acked` (narrowest grant); the checkbox flips
// `plugins.trust_unverified` — the global "don't ask again". Shared by the
// install-by-URL dialog and the Discover one-click path (which previously had no
// confirm at all).
export function TrustAckDialog({
  source,
  onConfirm,
  onClose,
}: {
  source: string; // the normalized repo the server reported (e.g. "github.com/owner/repo")
  onConfirm: (trustAll: boolean) => void;
  onClose: () => void;
}) {
  const [trustAll, setTrustAll] = useState(false);
  return (
    <ConfirmDialog
      open
      title="This plugin runs code on your machine"
      confirmLabel="Trust and install"
      onConfirm={() => onConfirm(trustAll)}
      onClose={onClose}
    >
      <div className="plugin-trust-ack">
        <p>
          <code>{source}</code> isn&apos;t an official source. Installing <strong>enables and runs
          its code immediately</strong> with the agent&apos;s full privileges — there is no sandbox.
          Only continue if you trust this repository; for untrusted code, use an MCP server instead.
        </p>
        <p>Confirming remembers this repository, so you won&apos;t be asked for it again.</p>
        <Checkbox
          checked={trustAll}
          onCheckedChange={(v: boolean) => setTrustAll(Boolean(v))}
          label="Don't ask again for any source (trust everything I install)"
        />
      </div>
    </ConfirmDialog>
  );
}
