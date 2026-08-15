import { Checkbox } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { useState, type ReactNode } from "react";

import { api } from "../lib/api";

// The one-time "this runs code" consent (ADR 0071 D3, #2721). The install route
// answers `needs_ack` for a source that is neither official nor previously acked;
// this dialog is the ack. Confirm persists the EXACT repo into
// `plugins.sources.acked` (narrowest grant); the checkbox flips
// `plugins.trust_unverified` — the global "don't ask again". Shared by the
// install-by-URL dialog and the Discover one-click path (which previously had no
// confirm at all).
// The whole needs_ack → confirm → POST /api/plugins/ack → retry flow, shared by the
// install-by-URL dialog and the Discover one-click path (the 2734 review flagged the
// copy-paste). A consumer calls `requestAck` with the target + its own retry, renders
// `ackDialog`, and routes failures/cancel through its own surface (status line or toast).
export function useTrustAck({
  onAckError,
  onCancel,
}: {
  onAckError: (message: string) => void;
  onCancel?: () => void;
}): {
  requestAck: (target: { url: string; source: string; retry: () => void }) => void;
  ackDialog: ReactNode;
} {
  const [pending, setPending] = useState<{ url: string; source: string; retry: () => void } | null>(null);
  const ackDialog = pending ? (
    <TrustAckDialog
      source={pending.source}
      onConfirm={async (trustAll) => {
        const target = pending;
        setPending(null);
        try {
          await api.ackPluginSource(target.url, trustAll);
        } catch (e) {
          onAckError(e instanceof Error ? e.message : "trust confirmation failed");
          return;
        }
        target.retry(); // now trusted — the retry installs for real
      }}
      onClose={() => {
        setPending(null);
        onCancel?.();
      }}
    />
  ) : null;
  return { requestAck: setPending, ackDialog };
}

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
