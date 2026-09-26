import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { Button } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { Dialog, useToast } from "@protolabsai/ui/overlays";

import { ImportSnapshotPanel } from "./ImportSnapshotPanel";
import { api } from "../lib/api";
import { ArchetypePicker } from "../setup/ArchetypePicker";
import { ArchetypeSetupForm } from "../setup/ArchetypeSetupForm";
import { pythonRuntimeView } from "../app/pythonRuntime";
import { archetypesQuery, fleetQuery, pythonRuntimeQuery, queryKeys } from "../lib/queries";
import { archetypeConfigFields, isMissingRequiredBundleConfig, requiresToolsNotice } from "../lib/archetypeConfig";
import {
  AGENT_NAME_RE,
  archetypeFlowReducer,
  createAgentBody,
  initialArchetypeFlow,
  suggestedAgentName,
} from "../lib/archetypeFlow";
import { escapeCloseAllowed, isTopmostOverlay } from "../lib/overlayStack";
import { HARD_GATE_HINT } from "../lib/pickerCopy";
import type { Archetype } from "../lib/types";

// Onboarding / archetype picker (ADR 0042), in two steps (lib/archetypeFlow):
//   1. PICK — the archetype cards only (ArchetypePicker): label, icon, blurb, "What's
//      included". No name, no config — one decision.
//   2. SET UP — a DS Dialog over the picker (ArchetypeSetupForm, shared with the Setup
//      Wizard): the name first (pre-filled with the archetype's suggested name), then the
//      bundle's questions, advanced options collapsed. Back returns to the picker with
//      every choice kept; Create posts. The Back/Create chrome is hand-assembled around a
//      DS Dialog until the DS has a stepper primitive (protoContent#520).
// Creating from a bundle clones+installs it (a few seconds) — the POST returns once the
// agent is up, so Create shows a spinner until then.
//
// A new agent has TWO sources (ADR 0091 #2106): an archetype (below) or a SNAPSHOT of an
// existing agent. They share this one entry point rather than living in separate places,
// because "where do new agents come from" should be one question with two answers. The
// snapshot path is its own component: it has to show a plan and take consent before it can
// create anything, which is a different shape from picking a card.
export function NewAgentPanel({
  onDone,
  onCancel,
}: {
  // `id` is the created agent's slug — FleetSurface navigates into the new agent's own
  // console with it. Optional because a success response may omit the agent record; the
  // caller must degrade (back to the list) rather than navigate to nowhere.
  onDone?: (name: string, id?: string) => void;
  onCancel?: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const archetypes = useQuery(archetypesQuery());
  const [flow, dispatch] = useReducer(archetypeFlowReducer, undefined, () => initialArchetypeFlow("basic"));
  // Which source this new agent comes from. Archetype is the default because it's the
  // common case; importing is deliberate and usually starts from a file you already have.
  const [source, setSource] = useState<"archetype" | "snapshot">("archetype");
  // Names already on the fleet — the suggested name steps around them (engineer-2, …).
  const fleet = useQuery({ ...fleetQuery(), refetchInterval: false });
  const taken = useMemo(() => (fleet.data?.agents ?? []).map((a) => a.name), [fleet.data]);

  // "custom" is a wizard-only persona (write-your-own SOUL) — this picker creates an
  // agent from a bundle, and its persona editor lives under Advanced, so Custom would
  // just duplicate Basic.
  const list = (archetypes.data?.archetypes ?? []).filter((a) => a.id !== "custom");
  const pickedArchetype = list.find((a) => a.id === flow.picked);
  const archetype = pickedArchetype ?? list[0];

  // The picked archetype's read-only peek — the source of the set-up form's fields (its
  // bundle's config_inputs, MCP inputs and declared secrets). Shares the preview dialog's
  // cache key; only fetched for bundle-backed archetypes (Basic has no bundle → no fields).
  const preview = useQuery({
    queryKey: ["archetype-preview", flow.picked],
    queryFn: () => api.archetypePreview(flow.picked),
    enabled: Boolean(pickedArchetype?.bundle),
    staleTime: 10 * 60 * 1000,
    retry: 1,
  });
  const fields = useMemo(() => archetypeConfigFields(preview.data), [preview.data]);

  // Runtime requirement at CHOOSE-time (#2186 follow-on): an archetype declaring
  // `requires: [python_runtime]` (cowork — its document skills route through
  // execute_code) gets a warning here when this host's managed runtime isn't ready,
  // so the ADR-0092 first-run doesn't end at a failed docx on the new agent's first
  // task. The new-agent flow is a HOST operation and the runtime is box-shared
  // (ADR 0094), so the host's runtime state is exactly the state the new agent gets.
  // `stale` (provisioned, old doc baseline) still works — no warning for it here.
  const pyRuntime = pythonRuntimeView(useQuery(pythonRuntimeQuery()).data);
  const runtimeWarning =
    pickedArchetype?.requires?.includes("python_runtime") && pyRuntime.kind === "action" && !pyRuntime.stale
      ? pyRuntime.installing
        ? `Python runtime is installing — ${pickedArchetype.label}'s document skills will work when it finishes.`
        : `${pickedArchetype.label} needs the managed Python runtime for its document skills — install it in Settings ▸ Tools first, or create the agent now and provision later.`
      : null;
  const contractNotice = pickedArchetype ? requiresToolsNotice(pickedArchetype.label, pickedArchetype.requires_tools) : null;
  const notices = [runtimeWarning, contractNotice].filter((n): n is string => Boolean(n));

  // A required bundle config_inputs answer is a HARD gate (#2977) — the server refuses the
  // create, so Create does too. MCP inputs / secrets stay soft (skip → env fallback).
  const missingHard = isMissingRequiredBundleConfig(fields, flow.values);
  const nameOk = AGENT_NAME_RE.test(flow.name.trim());
  const nameError =
    flow.name.trim() && !nameOk ? "Use only letters, numbers, dashes and underscores." : null;

  function pick(a: Archetype) {
    dispatch({ type: "pick", id: a.id, suggestedName: suggestedAgentName(a, taken), soul: a.soul ?? "" });
  }
  function next() {
    if (!archetype) return;
    pick(archetype); // same card → only fills a still-empty name/persona
    dispatch({ type: "next" });
  }

  const create = useMutation({
    // Carry the archetype's base SOUL (or the operator's edit of it) so a bundle agent
    // arrives WITH its persona, not just its tools (ADR 0042), plus every answer given —
    // bundle config answers, and the advanced MCP inputs / secrets. Blank answers are
    // dropped, so what was skipped still falls back to the host's environment / defaults.
    mutationFn: () => api.createAgent(createAgentBody(flow, archetype, fields)),
    onError: (e: Error) => toast({ tone: "error", title: "Couldn't create agent", message: e.message }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      const created = res.agent?.name ?? flow.name.trim();
      toast({ tone: "success", title: "Agent created", message: `${created} is ready.` });
      // Same guard as the name above: a success response without the agent record must
      // not throw here — it hands back no id and the caller falls back to the list.
      onDone?.(created, res.agent?.id);
    },
  });
  // The bundle's questions are unknown until its peek lands (`fields` is empty meanwhile,
  // so `missingHard` reads false) — hold Create (button AND Enter) until they are, or a
  // fast click posts past the required config_inputs and eats the server's #2977 refusal.
  const peekLoading = Boolean(pickedArchetype?.bundle) && preview.isLoading;
  const canCreate = nameOk && !missingHard && !peekLoading && !create.isPending;
  const submit = () => {
    if (canCreate) create.mutate();
  };
  const setupOpen = source === "archetype" && flow.step === "setup" && Boolean(archetype);

  // Esc / backdrop on the set-up dialog = Back. The DS Dialog closes on EVERY open
  // dialog's Escape, so an Escape aimed at a layer above this one (the folder picker's
  // dialog, the delegate dropdown) must not also send the operator back — sampled on
  // window capture, one-shot (lib/overlayStack, protoContent#521).
  const escapeNotOurs = useRef(false);
  useEffect(() => {
    if (!setupOpen) return;
    const sample = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        escapeNotOurs.current = !escapeCloseAllowed() || !isTopmostOverlay(".archetype-setup-dialog");
      }
    };
    window.addEventListener("keydown", sample, true);
    return () => window.removeEventListener("keydown", sample, true);
  }, [setupOpen]);
  const back = useCallback(() => {
    const notOurs = escapeNotOurs.current;
    escapeNotOurs.current = false;
    if (!notOurs) dispatch({ type: "back" });
  }, []);

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="New agent"
        kicker="pick an archetype, then name and set it up — a new workspace agent on this host"
        actions={
          onCancel ? (
            <Button variant="ghost" onClick={onCancel}>
              <ArrowLeft size={15} /> Back
            </Button>
          ) : undefined
        }
      />
      <div className="stage-body">
        <div className="new-agent-source" role="tablist" aria-label="New agent source">
          <button
            type="button"
            role="tab"
            aria-selected={source === "archetype"}
            className={source === "archetype" ? "is-active" : ""}
            onClick={() => setSource("archetype")}
          >
            From an archetype
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={source === "snapshot"}
            className={source === "snapshot" ? "is-active" : ""}
            onClick={() => setSource("snapshot")}
          >
            From a snapshot
          </button>
        </div>
        {source === "snapshot" ? (
          <ImportSnapshotPanel onDone={onDone} />
        ) : (
          <>
            <p className="fleet-section-label">Archetype</p>
            {/* Installed bundles grow this list without bound (#2193) — the cards scroll
                inside their own container so Next below never leaves the viewport. Height
                only: width stays with the AppShell's controlled container. */}
            <div className="archetype-card-scroll" style={{ maxHeight: "min(52vh, 560px)", overflowY: "auto" }}>
              <ArchetypePicker archetypes={list} value={flow.picked} onPick={pick} notices={notices} />
            </div>
            <div className="panel-actions archetype-step-actions">
              <Button variant="primary" disabled={!archetype} onClick={next}>
                Next
                <ChevronRight size={15} />
              </Button>
            </div>
          </>
        )}
      </div>
      {setupOpen && archetype ? (
        <Dialog
          open
          onClose={back}
          title={`Set up ${archetype.label}`}
          width="min(560px, 100%)"
          className="archetype-setup-dialog"
          footer={
            <>
              <Button variant="ghost" type="button" onClick={() => dispatch({ type: "back" })}>
                <ChevronLeft size={15} />
                Back
              </Button>
              <Button variant="primary" type="button" disabled={!canCreate} onClick={submit}>
                {create.isPending ? "Creating…" : archetype.bundle ? `Create from ${archetype.label}` : "Create agent"}
              </Button>
            </>
          }
        >
          <ArchetypeSetupForm
            name={flow.name}
            onNameChange={(name) => dispatch({ type: "setName", name })}
            nameHint="Letters, numbers, dashes and underscores — it's the agent's id and URL."
            nameError={nameError}
            onSubmit={submit}
            fields={fields}
            values={flow.values}
            onValueChange={(id, value) => dispatch({ type: "setValue", id, value })}
            soul={flow.soul}
            onSoulChange={(soul) => dispatch({ type: "setSoul", soul })}
            hardGateHint={HARD_GATE_HINT}
            loading={peekLoading}
          />
        </Dialog>
      ) : null}
    </section>
  );
}
