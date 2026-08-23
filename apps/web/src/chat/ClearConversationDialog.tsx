import { useEffect, useState } from "react";
import { Switch } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";

// The "Clear this conversation?" confirm for the ⌘K chat.clear keybinding and the /clear
// slash command (#2996). Clearing wipes the WHOLE conversation, so — like tab-close — it's
// gated behind a confirm with an opt-in "Harvest to knowledge" checkbox. On confirm it reports
// the harvest choice; the caller (ChatSurface) does the destructive deleteChatSession +
// updateMessages, keeping the tab open. Its own small component (rather than inline in the
// giant ChatSurface) so the confirm / cancel / harvest wiring is unit-testable in the console's
// `.test.ts`-only harness.
export function ClearConversationDialog({
  open,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  // Fired on confirm with the harvest opt-in state — the caller performs the actual wipe.
  onConfirm: (harvest: boolean) => void;
  onCancel: () => void;
}) {
  const [harvest, setHarvest] = useState(false);
  // Reset the opt-in whenever the dialog (re)opens, so a prior tick never carries into the
  // next clear — mirrors ChatSurface resetting harvestOnDelete before each close dialog.
  useEffect(() => {
    if (open) setHarvest(false);
  }, [open]);
  return (
    <ConfirmDialog
      open={open}
      title="Clear this conversation?"
      confirmLabel="Clear conversation"
      destructive
      onConfirm={() => onConfirm(harvest)}
      onClose={onCancel}
    >
      <p style={{ margin: 0 }}>Clear this conversation? This cannot be undone.</p>
      {/* Harvest is OPT-IN, mirroring delete: clearing must not silently copy the chat into
          searchable memory — the operator may be clearing it precisely to be rid of it. */}
      <Switch
        className="chat-delete-harvest"
        checked={harvest}
        onCheckedChange={setHarvest}
        label="Harvest into the knowledge base first (keeps a searchable summary)"
      />
    </ConfirmDialog>
  );
}
