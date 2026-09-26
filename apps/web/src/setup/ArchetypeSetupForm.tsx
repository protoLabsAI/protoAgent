import { useId, useState } from "react";
import type { ReactNode } from "react";

import { FormField, Input, Textarea } from "@protolabsai/ui/forms";
import { Accordion, AccordionItem } from "@protolabsai/ui/navigation";

import {
  fieldId,
  hasHardRequiredBundleConfig,
  isMissingRequiredBundleConfig,
  isMissingRequiredConfig,
  type ConfigField,
} from "../lib/archetypeConfig";
import {
  ADVANCED_CONNECTIONS_HELP,
  SETUP_OPTIONAL_HELP,
  SETUP_REQUIRED_HELP,
  SOFT_GATE_HINT,
  SOFT_GATE_HINT_COLLAPSED,
} from "../lib/pickerCopy";
import { ArchetypeConfigField } from "./ArchetypeConfigField";

// Step 2 of creating an agent from an archetype — shared by the fleet New-agent panel
// (inside a DS Dialog) and the Setup Wizard (as its own wizard step), so one component
// renders the set-up in both places:
//
//   1. Name first (pre-filled by the caller with the archetype's suggested name).
//   2. The bundle's `config_inputs` — the questions its author wrote for the operator —
//      as real form fields: a SHORT label, the optional `help` line under it, a folder
//      picker for `type: path`, a labelled switch for booleans. The "optional — leave
//      blank …" note is said ONCE above the group, not per field.
//   3. Advanced, collapsed: the bundle's MCP-server inputs + declared secrets (every one
//      falls back to this host's environment when blank) and the persona (SOUL.md).
//
// Stateless: the caller owns name / values / soul and the terminal action (Create,
// Next). `onSubmit` is Enter in the name field.
export function ArchetypeSetupForm({
  nameLabel = "Name",
  name,
  onNameChange,
  nameHint,
  nameError,
  onSubmit,
  identityExtra,
  fields,
  values,
  onValueChange,
  soul,
  onSoulChange,
  hardGateHint,
  loading,
}: {
  nameLabel?: string;
  name: string;
  onNameChange: (name: string) => void;
  nameHint?: ReactNode;
  // Shown instead of the hint when the name is invalid.
  nameError?: string | null;
  onSubmit?: () => void;
  // Extra identity fields rendered right after the name (the wizard's Operator).
  identityExtra?: ReactNode;
  fields: ConfigField[];
  values: Record<string, string>;
  onValueChange: (id: string, value: string) => void;
  soul: string;
  onSoulChange: (soul: string) => void;
  // The hard-gate copy names the caller's terminal action ("created" / "setup can finish").
  hardGateHint: string;
  // The bundle peek is still loading — its questions aren't known yet.
  loading?: boolean;
}) {
  const nameId = useId();
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // The bundle's own questions lead; MCP inputs + declared secrets (env-fallback
  // plumbing) go under Advanced.
  const questions = fields.filter((f) => f.origin === "config");
  const connections = fields.filter((f) => f.origin !== "config");
  const missingHard = isMissingRequiredBundleConfig(fields, values);
  const missingSoft = isMissingRequiredConfig(connections, values);
  const hasHardRequired = hasHardRequiredBundleConfig(questions);

  const renderField = (f: ConfigField) => {
    const id = fieldId(f);
    const value = values[id] ?? "";
    const onChange = (v: string) => onValueChange(id, v);
    if (f.kind === "boolean") {
      // A switch carries its own label; a FormField <label> around it would nest labels.
      const helpId = f.help ? `${id}:help` : undefined;
      return (
        <div key={id} className="pl-field archetype-setup-switch">
          <ArchetypeConfigField field={f} value={value} onChange={onChange} describedBy={helpId} />
          {f.help ? (
            <span id={helpId} className="pl-field__hint">
              {f.help}
            </span>
          ) : null}
        </div>
      );
    }
    return (
      <FormField key={id} label={`${f.label}${f.required ? " *" : ""}`} hint={f.help}>
        <ArchetypeConfigField field={f} value={value} onChange={onChange} />
      </FormField>
    );
  };

  return (
    <div className="archetype-setup">
      <FormField
        label={nameLabel}
        hint={nameError ? <span className="archetype-setup-error">{nameError}</span> : nameHint}
      >
        <Input
          id={nameId}
          value={name}
          autoFocus
          aria-label="Agent name"
          aria-invalid={nameError ? true : undefined}
          placeholder="e.g. ava, roxy, research-bot"
          onChange={(e) => onNameChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onSubmit?.();
          }}
        />
      </FormField>
      {identityExtra}

      {loading ? <p className="archetype-setup-help">Reading the archetype&apos;s setup…</p> : null}
      {questions.length ? (
        <section className="archetype-setup-group" aria-label="Archetype settings">
          <p className="archetype-setup-help">{hasHardRequired ? SETUP_REQUIRED_HELP : SETUP_OPTIONAL_HELP}</p>
          {questions.map(renderField)}
        </section>
      ) : null}
      {missingHard ? (
        <p className="archetype-setup-help archetype-setup-gate" role="status">
          {hardGateHint}
        </p>
      ) : null}

      <Accordion className="archetype-setup-advanced">
        <AccordionItem title="Advanced" open={advancedOpen} onOpenChange={setAdvancedOpen}>
          <div className="archetype-setup-group">
            {connections.length ? (
              <>
                <p className="archetype-setup-help">{ADVANCED_CONNECTIONS_HELP}</p>
                {connections.map(renderField)}
                {missingSoft ? <p className="archetype-setup-help">{SOFT_GATE_HINT}</p> : null}
              </>
            ) : null}
            <FormField label="Persona (SOUL.md)" hint="The agent's base persona — seeded from the archetype. Edit freely.">
              <Textarea
                className="archetype-setup-soul"
                value={soul}
                placeholder="Leave blank for the default persona."
                onChange={(e) => onSoulChange(e.target.value)}
              />
            </FormField>
          </div>
        </AccordionItem>
      </Accordion>
      {missingSoft && !advancedOpen ? <p className="archetype-setup-help">{SOFT_GATE_HINT_COLLAPSED}</p> : null}
    </div>
  );
}
