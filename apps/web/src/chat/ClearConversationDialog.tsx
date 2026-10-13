import { useEffect, useState } from "react";
import { Switch } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";

import type { ChatMemoryChoice } from "./sessionRetirement";

// What deleting or clearing a chat can do to memory: the harvest / forget switches and the
// note that keeps them honest (#3493). Shared by the "Delete this chat?" dialog in ChatSurface
// and the "Clear this conversation?" dialog below, so the two can't drift apart.
//
// Harvest defaults ON for an ordinary chat (#4053): the operator's "keep this out of memory"
// decision is incognito (ADR 0069 D3b), so an ordinary delete auto-harvests unless they turn
// it off. The harvest switch only ADDS a summary — it never controlled compaction, which
// archives a long chat's earlier messages on its own (auto-compaction, /compact) — so the
// note says that, and the forget switch is the one that removes those archives along with the
// summaries and facts harvested from this chat. Harvest and forget are mutually exclusive:
// ticking one unticks the other, so "just make it gone" (forget, no harvest) is one click.
//
// For an incognito chat the harvest switch is absent entirely (it is never harvested, here or
// on the server) and a short note stands in its place; forget still shows.
export function ChatMemoryChoices({
  action,
  incognito,
  harvest,
  forget,
  onHarvestChange,
  onForgetChange,
}: {
  action: "Deleting" | "Clearing";
  incognito?: boolean;
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
      {incognito ? (
        // No harvest switch for an incognito chat — it is never harvested (ADR 0069 D3b),
        // and harvest is always sent false. The word "harvested" is fine here because the
        // harvest switch itself is absent (the e2e /Harvest/ locator targets that switch).
        <p className="chat-memory-note chat-delete-incognito-note">
          Incognito chat — never harvested into the knowledge base.
        </p>
      ) : (
        // Harvest defaults ON (#4053); ticking it unticks forget. Exclusion is enforced here
        // (not in each owner) so both dialogs behave identically and can't drift.
        <Switch
          className="chat-delete-harvest"
          checked={harvest}
          onCheckedChange={(checked) => {
            onHarvestChange(checked);
            if (checked) onForgetChange(false);
          }}
          label="Harvest into the knowledge base first (adds a searchable summary)"
        />
      )}
      {/* Forget removes what the chat already wrote to memory; ticking it unticks harvest. */}
      <Switch
        className="chat-delete-forget"
        checked={forget}
        onCheckedChange={(checked) => {
          onForgetChange(checked);
          if (checked) onHarvestChange(false);
        }}
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
  incognito,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  // Whether the target chat is incognito — gates the harvest default and switch (#4053).
  incognito?: boolean;
  // Fired on confirm with the memory choices — the caller performs the actual wipe.
  onConfirm: (memory: ChatMemoryChoice) => void;
  onCancel: () => void;
}) {
  // Harvest defaults ON for an ordinary chat, OFF (and switchless) for an incognito one (#4053).
  const [harvest, setHarvest] = useState(false);
  const [forget, setForget] = useState(false);
  // Re-initialise both switches whenever the dialog (re)opens, from THIS chat's incognito flag,
  // so a prior tick never carries into the next clear — mirrors ChatSurface's reset points.
  useEffect(() => {
    if (open) {
      setHarvest(!incognito);
      setForget(false);
    }
  }, [open, incognito]);
  return (
    <ConfirmDialog
      open={open}
      title="Clear this conversation?"
      confirmLabel="Clear conversation"
      destructive
      // An incognito chat is never harvested, whatever the (absent) switch would read.
      onConfirm={() => onConfirm({ harvest: incognito ? false : harvest, forget })}
      onClose={onCancel}
    >
      <p style={{ margin: 0 }}>Clear this conversation? This cannot be undone.</p>
      <ChatMemoryChoices
        action="Clearing"
        incognito={incognito}
        harvest={harvest}
        forget={forget}
        onHarvestChange={setHarvest}
        onForgetChange={setForget}
      />
    </ConfirmDialog>
  );
}
