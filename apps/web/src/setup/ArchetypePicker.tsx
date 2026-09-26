import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import { RadioCard, RadioCardGroup } from "@protolabsai/ui/forms";

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
// The per-card "What's included" link sits BESIDE the DS RadioCard, not inside it: the card
// is a <label>, and a button inside a label is invalid (both are labelable) — the DS card
// has no action slot yet (protoContent#522, see the TODO below).
export function ArchetypePicker({
  archetypes,
  value,
  onPick,
  notices = [],
  name = "archetype",
}: {
  archetypes: Archetype[];
  value: string;
  onPick: (a: Archetype) => void;
  // Choose-time notes about the picked card (runtime requirement, capability contract).
  notices?: string[];
  name?: string;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(() =>
    archetypes.some((a) => a.id === value && a.tier === "advanced"),
  );
  const [previewing, setPreviewing] = useState<Archetype | null>(null);
  const standard = archetypes.filter((a) => a.tier !== "advanced");
  const advanced = archetypes.filter((a) => a.tier === "advanced");
  const select = (id: string) => {
    const a = archetypes.find((x) => x.id === id);
    if (a) onPick(a);
  };

  // TODO(protoContent#522 RadioCard action slot): render the link inside the card once the DS
  // RadioCard grows a footer/action slot, and drop the .archetype-card wrapper.
  const cards = (list: Archetype[]) =>
    list.map((a) => (
      <div key={a.id} className="archetype-card">
        <RadioCard value={a.id} icon={lucideIcon(a.icon, 22)} title={a.label} blurb={a.blurb} />
        <button
          type="button"
          className="archetype-preview-link"
          aria-label={`What's included in ${a.label}`}
          onClick={() => setPreviewing(a)}
        >
          What&apos;s included →
        </button>
      </div>
    ));

  return (
    <div className="archetype-picker">
      <RadioCardGroup name={name} min="180px" value={value} onValueChange={select}>
        {cards(standard)}
      </RadioCardGroup>
      {advanced.length ? (
        <div className="archetype-advanced">
          <button
            type="button"
            className="archetype-configure-toggle"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((o) => !o)}
          >
            {advancedOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            <span>Advanced ({advanced.length})</span>
          </button>
          {advancedOpen ? (
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
