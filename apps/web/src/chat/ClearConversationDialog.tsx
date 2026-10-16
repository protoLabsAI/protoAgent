import { useEffect, useState } from "react";
import { Radio, RadioGroup } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";

import { defaultMemoryChoice, type ChatMemoryChoice } from "./sessionRetirement";

// What deleting or clearing a chat does to memory, as ONE plain-language choice. The two
// mutually-exclusive harvest/forget switches (#4053/#3493) were really a single 3-way pick, so
// they are now a radio group: "Save a summary" (harvest), "Don't save anything" (neither), or
// "Erase from memory" (forget). The radio makes the exclusion structural — you can't pick two —
// and each option carries its own one-line explanation, so the old compaction paragraph and its
// jargon ("harvest", "compaction") are gone: #3493's honesty requirement lives in the "Don't
// save anything … already archived from this chat stays" / "Erase … also removes … archives
// this chat already saved" copy instead. Shared by the "Delete this chat?" dialog in ChatSurface
// and the "Clear this conversation?" dialog below, so the two can't drift apart.
//
// Defaults: "Save a summary" for an ordinary chat (the #4053 default-on harvest), "Don't save
// anything" for an incognito chat. An incognito chat is never saved to memory (ADR 0069 D3b),
// so the "Save a summary" option is not rendered and a one-line note stands above the other two;
// harvest is forced false for it, here and on the server.
export type ChatMemoryValue = "summary" | "none" | "erase";

function toValue(choice: ChatMemoryChoice): ChatMemoryValue {
  if (choice.forget) return "erase";
  if (choice.harvest) return "summary";
  return "none";
}

function toChoice(value: ChatMemoryValue): ChatMemoryChoice {
  switch (value) {
    case "summary":
      return { harvest: true, forget: false };
    case "erase":
      return { harvest: false, forget: true };
    default:
      return { harvest: false, forget: false };
  }
}

function hint(text: string) {
  return <span className="chat-memory-choice-hint">{text}</span>;
}

export function ChatMemoryChoices({
  value,
  onChange,
  incognito,
}: {
  // The current memory choice, mapped to/from the radio value inside this component.
  value: ChatMemoryChoice;
  onChange: (choice: ChatMemoryChoice) => void;
  // Whether the target chat is incognito — drops the "Save a summary" option (#4053, ADR 0069 D3b).
  incognito?: boolean;
}) {
  return (
    <>
      <p className="chat-memory-label">Memory</p>
      {incognito ? (
        // No "Save a summary" option for an incognito chat — it is never saved to memory, here or
        // on the server. A one-line note stands above the remaining two options.
        <p className="chat-memory-note chat-delete-incognito-note">Incognito — never saved to memory.</p>
      ) : null}
      <RadioGroup
        className="chat-memory-choice"
        value={toValue(value)}
        onValueChange={(v) => onChange(toChoice(v as ChatMemoryValue))}
      >
        {incognito ? null : (
          <Radio
            value="summary"
            label={
              <>
                <strong>Save a summary</strong>
                {hint("Adds a searchable summary to the knowledge base.")}
              </>
            }
          />
        )}
        <Radio
          value="none"
          label={
            <>
              <strong>Don't save anything</strong>
              {hint("Anything already archived from this chat stays.")}
            </>
          }
        />
        <Radio
          value="erase"
          label={
            <>
              <strong>Erase from memory</strong>
              {hint("Also removes summaries and archives this chat already saved.")}
            </>
          }
        />
      </RadioGroup>
    </>
  );
}

// The "Clear this conversation?" confirm for the ⌘K chat.clear keybinding and the /clear
// slash command (#2996). Clearing wipes the WHOLE conversation, so — like tab-close — it's
// gated behind a confirm with the same 3-way memory choice as delete. On confirm it reports the
// selected choice; the caller (ChatSurface) awaits the non-retiring server clear before wiping
// local messages, keeping the tab open. Its own small component (rather than inline in the giant
// ChatSurface) so the confirm / cancel / memory wiring is unit-testable in the console's
// `.test.ts`-only harness.
export function ClearConversationDialog({
  open,
  incognito,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  // Whether the target chat is incognito — gates the default choice and the "Save a summary" option.
  incognito?: boolean;
  // Fired on confirm with the memory choice — the caller performs the actual wipe.
  onConfirm: (memory: ChatMemoryChoice) => void;
  onCancel: () => void;
}) {
  // "Save a summary" for an ordinary chat, "Don't save anything" for an incognito one (#4053).
  const [memory, setMemory] = useState<ChatMemoryChoice>(() => defaultMemoryChoice(incognito));
  // Re-initialise the choice whenever the dialog (re)opens, from THIS chat's incognito flag, so a
  // prior pick never carries into the next clear — mirrors ChatSurface's reset points.
  useEffect(() => {
    if (open) setMemory(defaultMemoryChoice(incognito));
  }, [open, incognito]);
  return (
    <ConfirmDialog
      open={open}
      title="Clear this conversation?"
      confirmLabel="Clear conversation"
      destructive
      // An incognito chat is never harvested, whatever the (absent) summary option would read.
      onConfirm={() => onConfirm(incognito ? { ...memory, harvest: false } : memory)}
      onClose={onCancel}
    >
      <p style={{ margin: 0 }}>This conversation will be cleared. This can't be undone.</p>
      <ChatMemoryChoices value={memory} onChange={setMemory} incognito={incognito} />
    </ConfirmDialog>
  );
}
