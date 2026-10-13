import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import { RadioCard, RadioCardGroup, Switch } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";

import { lucideIcon } from "../lib/lucideIcon";
import type { Archetype } from "../lib/types";
import { ArchetypePreviewDialog } from "./ArchetypePreviewDialog";

// Step 1 of creating an agent from an archetype — shared by the fleet New-agent panel and
// the Setup Wizard. Cards ONLY: label, icon, blurb, and a "What's included" link per card.
// No name and no config here — those are step 2 (ArchetypeSetupForm), so the picker reads
// as one decision instead of a form that keeps growing below the cards.
//
// Advanced archetypes (tier: "advanced") collapse behind an "Advanced (N)" toggle — a
// second RadioCardGroup sharing the same value + onPick, so choosing one is identical to
// choosing a standard card. Hidden entirely when the catalog has none.
//
// Held archetypes (the catalog's `held` entries, still being tested) are opt-in: a caller
// passing `previewArchetypes` gets a "Show preview archetypes" switch at the top of the
// Advanced section (which then always renders), and when it's on the held cards join the
// advanced group with a "Preview" badge. Without that prop a held row never renders — the
// Setup Wizard doesn't offer them.
//
// The per-card "What's included" link sits BESIDE the DS RadioCard, not inside it: the card
// is a <label>, and a button inside a label is invalid (both are labelable) — the DS card
// has no action slot yet (protoContent#522, see the TODO below).
export function ArchetypePicker({
  archetypes,
  value,
  onPick,
  notices = [],
  name = "archetype",
  previewArchetypes,
}: {
  archetypes: Archetype[];
  value: string;
  onPick: (a: Archetype) => void;
  // Choose-time notes about the picked card (runtime requirement, capability contract).
  notices?: string[];
  name?: string;
  // The opt-in for held (preview) archetypes — the caller owns the persisted pref and the
  // fetch that includes them; this only renders the switch and the badged cards.
  previewArchetypes?: { on: boolean; onToggle: (on: boolean) => void };
}) {
  const showHeld = Boolean(previewArchetypes?.on);
  const visible = archetypes.filter((a) => !a.held || showHeld);
  const [advancedOpen, setAdvancedOpen] = useState(() =>
    visible.some((a) => a.id === value && (a.tier === "advanced" || a.held)),
  );
  const [previewing, setPreviewing] = useState<Archetype | null>(null);
  const standard = visible.filter((a) => a.tier !== "advanced" && !a.held);
  // Held cards file under Advanced too, after the advanced tier — opting in is an advanced act.
  const advanced = [...visible.filter((a) => a.tier === "advanced" && !a.held), ...visible.filter((a) => a.held)];
  const select = (id: string) => {
    const a = visible.find((x) => x.id === id);
    if (a) onPick(a);
  };

  // TODO(protoContent#522 RadioCard action slot): render the link inside the card once the DS
  // RadioCard grows a footer/action slot, and drop the .archetype-card wrapper.
  const cards = (list: Archetype[]) =>
    list.map((a) => (
      <div key={a.id} className="archetype-card">
        <RadioCard
          value={a.id}
          icon={lucideIcon(a.icon, 22)}
          title={
            a.held ? (
              <span className="archetype-card-title">
                {a.label} <Badge status="info">Preview</Badge>
              </span>
            ) : (
              a.label
            )
          }
          blurb={a.blurb}
        />
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={`What's included in ${a.label}`}
          onClick={() => setPreviewing(a)}
        >
          What&apos;s included →
        </Button>
      </div>
    ));

  return (
    <div className="archetype-picker">
      <RadioCardGroup name={name} min="180px" value={value} onValueChange={select}>
        {cards(standard)}
      </RadioCardGroup>
      {advanced.length || previewArchetypes ? (
        <div className="archetype-advanced">
          <button
            type="button"
            className="archetype-configure-toggle"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((o) => !o)}
          >
            {advancedOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            {/* No count when the section only holds the preview switch (no advanced cards). */}
            <span>{advanced.length ? `Advanced (${advanced.length})` : "Advanced"}</span>
          </button>
          {advancedOpen && previewArchetypes ? (
            <div className="archetype-preview-toggle">
              <Switch
                checked={previewArchetypes.on}
                onCheckedChange={previewArchetypes.onToggle}
                label="Show preview archetypes"
              />
              <span className="archetype-preview-muted">
                Archetypes still being tested — they may change or break. Remembered on this console.
              </span>
            </div>
          ) : null}
          {advancedOpen && advanced.length ? (
            <RadioCardGroup name={`${name}-advanced`} min="180px" value={value} onValueChange={select}>
              {cards(advanced)}
            </RadioCardGroup>
          ) : null}
        </div>
      ) : null}
      {notices.map((n) => (
        <p key={n} className="archetype-runtime-notice" role="note">
          {n}
        </p>
      ))}
      {previewing ? <ArchetypePreviewDialog archetype={previewing} onClose={() => setPreviewing(null)} /> : null}
    </div>
  );
}
