import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, ChevronRight } from "lucide-react";
import { useMemo, useState } from "react";

import { Input, RadioCard, RadioCardGroup, SecretInput } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";

import { api } from "../lib/api";
import { ArchetypePreviewDialog } from "../setup/ArchetypePreviewDialog";
import { archetypesQuery, queryKeys } from "../lib/queries";
import { lucideIcon } from "../lib/lucideIcon";
import { archetypeConfigFields, isMissingRequiredConfig, fieldId, splitConfigValues } from "../lib/archetypeConfig";
import type { Archetype } from "../lib/types";

const NAME_RE = /^[A-Za-z0-9-_]+$/;

// Onboarding / archetype picker (ADR 0042). Name the agent, pick an archetype (Basic +
// installed bundles), optionally configure the bundle's MCP inputs + secrets (#2041), create.
// Name + Create sit ABOVE the archetype section (#2193) so a growing archetype list never
// pushes them off-screen — the card list scrolls inside its own bounded container instead.
// Creating from a bundle clones+installs it (a few seconds) — the POST returns once the
// agent is up, so the button shows a spinner until then.
export function NewAgentPanel({ onDone, onCancel }: { onDone?: (name: string) => void; onCancel?: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const archetypes = useQuery(archetypesQuery());
  const [picked, setPicked] = useState<string>("basic");
  const [name, setName] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  // The inline Configure step: expanded by default when the archetype has inputs, so the
  // operator sees what to fill; collapsing skips it (→ env-only seed). `values` is keyed by
  // fieldId(origin+key) so an MCP input and a declared secret sharing a key don't collide.
  const [configOpen, setConfigOpen] = useState(true);
  const [values, setValues] = useState<Record<string, string>>({});

  // "custom" is a wizard-only persona (write-your-own SOUL) — this picker creates an
  // agent from a bundle and has no SOUL editor, so Custom would just duplicate Basic.
  const list = (archetypes.data?.archetypes ?? []).filter((a) => a.id !== "custom");
  const pickedArchetype = list.find((a) => a.id === picked);
  const archetype = pickedArchetype ?? list[0];
  const nameOk = NAME_RE.test(name);

  // The picked archetype's read-only peek — the source of the Configure form's fields (its
  // bundle's MCP inputs + declared secrets). Shares the dialog's cache key; only fetched for
  // bundle-backed archetypes (Basic has no bundle → no form, backward compatible).
  const preview = useQuery({
    queryKey: ["archetype-preview", picked],
    queryFn: () => api.archetypePreview(picked),
    enabled: Boolean(pickedArchetype?.bundle),
    staleTime: 10 * 60 * 1000,
    retry: 1,
  });
  const fields = useMemo(() => archetypeConfigFields(preview.data), [preview.data]);
  // A required field left blank is a soft hint, NOT a hard gate — skipping the Configure step
  // (or an individual required field) is a first-class path that falls back to env-only.
  const missingRequired = configOpen && isMissingRequiredConfig(fields, values);

  function pick(id: string) {
    setPicked(id);
    setValues({}); // a token typed for one archetype must not carry into the next
    setConfigOpen(true);
  }

  const create = useMutation({
    // Carry the archetype's base SOUL so a bundle agent arrives WITH its persona, not just
    // its tools (ADR 0042). Blank soul (bundle with no inline persona) → server leaves the
    // agent on the default SOUL. When the Configure form is open, split the collected values
    // into the two seed channels (#2041); a collapsed/absent form sends nothing → env-only.
    mutationFn: () => {
      const { inputs, secrets } =
        configOpen && fields.length ? splitConfigValues(fields, values) : { inputs: {}, secrets: [] };
      return api.createAgent({
        name: name.trim(),
        bundle: archetype?.bundle ?? null,
        soul: archetype?.soul || undefined,
        inputs: Object.keys(inputs).length ? inputs : undefined,
        secrets: secrets.length ? secrets : undefined,
      });
    },
    onError: (e: Error) => toast({ tone: "error", title: "Couldn't create agent", message: e.message }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      const created = res.agent?.name ?? name.trim();
      toast({ tone: "success", title: "Agent created", message: `${created} is ready.` });
      onDone?.(created);
    },
  });

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="New agent"
        kicker="name it, pick an archetype, and launch — a new workspace agent on this host"
        actions={
          onCancel ? (
            <Button variant="ghost" onClick={onCancel}>
              <ArrowLeft size={15} /> Back
            </Button>
          ) : undefined
        }
      />
      <div className="stage-body">
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

        <p className="fleet-section-label">Archetype</p>
        {/* Installed bundles grow this list without bound (#2193) — the cards scroll inside
            their own container so Name/Create above never leave the viewport. Height only:
            width stays with the AppShell's controlled container. */}
        <div className="archetype-card-scroll" style={{ maxHeight: "min(40vh, 420px)", overflowY: "auto" }}>
          <RadioCardGroup name="archetype" min="160px" value={picked} onValueChange={pick}>
            {list.map((a: Archetype) => (
              <RadioCard key={a.id} value={a.id} icon={lucideIcon(a.icon, 22)} title={a.label} blurb={a.blurb} />
            ))}
          </RadioCardGroup>
        </div>
        {pickedArchetype ? (
          <button type="button" className="archetype-preview-link" onClick={() => setPreviewOpen(true)}>
            See what&apos;s included in {pickedArchetype.label} →
          </button>
        ) : null}
        {previewOpen && pickedArchetype ? (
          <ArchetypePreviewDialog archetype={pickedArchetype} onClose={() => setPreviewOpen(false)} />
        ) : null}

        {/* Inline Configure step (#2041) — appears only when the picked bundle has MCP inputs
            or declared secrets. Collapsible: skipping falls back to this host's environment. */}
        {fields.length ? (
          <div className="archetype-configure">
            <button
              type="button"
              className="archetype-configure-toggle"
              aria-expanded={configOpen}
              onClick={() => setConfigOpen((o) => !o)}
            >
              {configOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              <span>Configure {pickedArchetype?.label}</span>
              <span className="field-hint">optional — skip to use this host's environment</span>
            </button>
            {configOpen ? (
              <div className="archetype-configure-fields">
                {fields.map((f) => (
                  <label key={fieldId(f)} className="field">
                    <span>
                      {f.label}
                      {f.required ? " *" : ""}
                    </span>
                    {f.secret ? (
                      <SecretInput
                        placeholder={f.placeholder}
                        value={values[fieldId(f)] ?? ""}
                        aria-label={f.label}
                        onChange={(e) => setValues((v) => ({ ...v, [fieldId(f)]: e.target.value }))}
                      />
                    ) : (
                      <Input
                        type="text"
                        placeholder={f.placeholder}
                        value={values[fieldId(f)] ?? ""}
                        aria-label={f.label}
                        onChange={(e) => setValues((v) => ({ ...v, [fieldId(f)]: e.target.value }))}
                      />
                    )}
                  </label>
                ))}
                {missingRequired ? (
                  <span className="field-hint">
                    Fields marked * connect their server — fill them, or skip to use this host's environment.
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
