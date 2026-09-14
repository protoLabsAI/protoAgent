import { useEffect, useState } from "react";
import { Switch } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";

import type { ChatMemoryChoice } from "./sessionRetirement";

// What deleting or clearing a chat can do to memory: the two opt-in switches and the note
// that keeps them honest (#3493). Shared by the "Delete this chat?" dialog in ChatSurface
// and the "Clear this conversation?" dialog below, so the two can't drift apart.
//
// The harvest switch only ADDS a summary. It never controlled compaction, which archives a
// long chat's earlier messages to the knowledge base on its own (auto-compaction, /compact)
// — so the note says that, and the forget switch is the one that removes those archives
// along with the summaries and facts harvested from this chat.
export function ChatMemoryChoices({
  action,
  harvest,
  forget,
  onHarvestChange,
  onForgetChange,
}: {
  action: "Deleting" | "Clearing";
  harvest: boolean;
  forget: boolean;
  onHarvestChange: (checked: boolean) => void;
  onForgetChange: (checked: boolean) => void;
}) {
  return (
    <>
      <p className="chat-memory-note">
        {`Parts of this chat may already be in the knowledge base: when a long chat fills the context window, or you run /compact, its earlier messages are archived there. ${action} the chat leaves them there unless you choose to forget them below.`}
      </p>
      {/* Harvest is OPT-IN: deleting a chat must not silently copy it into searchable
          memory — the operator may be deleting it precisely to get rid of it. */}
      <Switch
        className="chat-delete-harvest"
        checked={harvest}
        onCheckedChange={onHarvestChange}
        label="Harvest into the knowledge base first (adds a searchable summary)"
      />
      {/* Forget is OPT-IN too: removing memory is destructive, and whether it should be the
          default is the operator's policy call, not this dialog's. */}
      <Switch
        className="chat-delete-forget"
        checked={forget}
        onCheckedChange={onForgetChange}
        label="Forget what this chat already saved to memory (its archived transcripts, summaries and facts)"
      />
    </>
  );
}

// The "Clear this conversation?" confirm for the ⌘K chat.clear keybinding and the /clear
// slash command (#2996). Clearing wipes the WHOLE conversation, so — like tab-close — it's
// gated behind a confirm with the same opt-in memory switches as delete. On confirm it
// reports those choices; the caller (ChatSurface) awaits the non-retiring server clear before
// wiping local messages, keeping the tab open. Its own small component (rather than inline in
// the giant ChatSurface) so the confirm / cancel / memory wiring is unit-testable in the
// console's `.test.ts`-only harness.
export function ClearConversationDialog({
  open,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  // Fired on confirm with the memory choices — the caller performs the actual wipe.
  onConfirm: (memory: ChatMemoryChoice) => void;
  onCancel: () => void;
}) {
  const [harvest, setHarvest] = useState(false);
  const [forget, setForget] = useState(false);
  // Reset both opt-ins whenever the dialog (re)opens, so a prior tick never carries into the
  // next clear — mirrors ChatSurface resetting its switches before each close dialog.
  useEffect(() => {
    if (open) {
      setHarvest(false);
      setForget(false);
    }
  }, [open]);
  return (
    <ConfirmDialog
      open={open}
      title="Clear this conversation?"
      confirmLabel="Clear conversation"
      destructive
      onConfirm={() => onConfirm({ harvest, forget })}
      onClose={onCancel}
    >
      <p style={{ margin: 0 }}>Clear this conversation? This cannot be undone.</p>
      <ChatMemoryChoices
        action="Clearing"
        harvest={harvest}
        forget={forget}
        onHarvestChange={setHarvest}
        onForgetChange={setForget}
      />
    </ConfirmDialog>
  );
}
