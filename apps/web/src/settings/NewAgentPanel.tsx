import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";

import { Input, RadioCard, RadioCardGroup } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { Alert } from "@protolabsai/ui/data";
import { PanelHeader } from "@protolabsai/ui/navigation";

import { api } from "../lib/api";
import { archetypesQuery, queryKeys } from "../lib/queries";
import { lucideIcon } from "../lib/lucideIcon";
import type { Archetype } from "../lib/types";

const NAME_RE = /^[A-Za-z0-9-_]+$/;

// Onboarding / archetype picker (ADR 0042). Pick an archetype (Basic + installed bundles),
// name the agent, create. Creating from a bundle clones+installs it (a few seconds) — the
// POST returns once the agent is up, so the button shows a spinner until then.
export function NewAgentPanel({ onDone, onCancel }: { onDone?: (name: string) => void; onCancel?: () => void }) {
  const qc = useQueryClient();
  const archetypes = useQuery(archetypesQuery());
  const [picked, setPicked] = useState<string>("basic");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  // "custom" is a wizard-only persona (write-your-own SOUL) — this picker creates an
  // agent from a bundle and has no SOUL editor, so Custom would just duplicate Basic.
  const list = (archetypes.data?.archetypes ?? []).filter((a) => a.id !== "custom");
  const archetype = list.find((a) => a.id === picked) ?? list[0];
  const nameOk = NAME_RE.test(name);

  const create = useMutation({
    mutationFn: () => api.createAgent({ name: name.trim(), bundle: archetype?.bundle ?? null }),
    onMutate: () => setError(null),
    onError: (e: Error) => setError(e.message),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      onDone?.(res.agent?.name ?? name.trim());
    },
  });

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="New agent"
        kicker="pick an archetype, name it, and launch — a new workspace agent on this host"
        actions={
          onCancel ? (
            <Button variant="ghost" onClick={onCancel}>
              <ArrowLeft size={15} /> Back
            </Button>
          ) : undefined
        }
      />
      <div className="stage-body">
        {error ? <Alert status="error">{error}</Alert> : null}

        <p className="fleet-section-label">Archetype</p>
        <RadioCardGroup name="archetype" min="160px" value={picked} onValueChange={setPicked}>
          {list.map((a: Archetype) => (
            <RadioCard key={a.id} value={a.id} icon={lucideIcon(a.icon, 22)} title={a.label} blurb={a.blurb} />
          ))}
        </RadioCardGroup>

        <label className="field archetype-name-field">
          <span>Name</span>
          <Input
            value={name}
            autoFocus
            placeholder="e.g. ava, roxy, research-bot"
            aria-label="Agent name"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && nameOk && !create.isPending) create.mutate();
            }}
          />
          <span className="field-hint">Letters, numbers, dashes and underscores — it's the agent's id and URL.</span>
        </label>

        <div className="panel-actions">
          <Button
            variant="primary"
            disabled={!nameOk || create.isPending}
            onClick={() => create.mutate()}
          >
            {create.isPending ? "Creating…" : archetype?.bundle ? `Create from ${archetype.label}` : "Create agent"}
          </Button>
        </div>
      </div>
    </section>
  );
}
