import { useQuery } from "@tanstack/react-query";
import { DropdownSelect, Field, FormField, Input, RadioCard, RadioCardGroup, Textarea } from "@protolabsai/ui/forms";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { Alert, Spinner } from "@protolabsai/ui/data";
import {
  Bot,
  Check,
  ChevronLeft,
  ChevronRight,
  Cpu,
  HardDrive,
  KeyRound,
  Search,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { TestConnectionButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { lucideIcon } from "../lib/lucideIcon";
import { acpAgentsQuery, archetypesQuery } from "../lib/queries";
import type { AgentConfig, Archetype, ConfigPayload } from "../lib/types";

// Four steps: intro, then "who the agent is" (name + persona), then "how it thinks"
// (the model/coding-agent runtime), then a summary. Identity + persona are one step.
type Step = "welcome" | "agent" | "brain" | "finish";

// Two former steps were dropped — they're all sensible defaults a new user shouldn't
// have to reason about; the values flow straight through finishSetup:
//   • Workspace (project dir / allowed dirs / knowledge db / top-K / tasks-init):
//     blank project dir = the protoAgent dir, blank knowledge db = default location,
//     top-K 5, no tasks init (do that from the Tasks view when there's a board).
//   • Tools (middleware toggles + researcher turns): all middleware on, 40 turns —
//     tune later in Settings.
const steps: Step[] = ["welcome", "agent", "brain", "finish"];


type WizardState = {
  agentName: string;
  operatorName: string;
  // Where turns run. "native" = the LangGraph loop on the gateway below; "acp" = hand
  // each turn to the chosen CLI coding agent (agent_runtime: acp:<acpAgent>) — ADR 0033.
  runtimeKind: "native" | "acp";
  acpAgent: string;
  apiBase: string;
  apiKey: string;
  modelName: string;
  temperature: number;
  maxTokens: number;
  maxIterations: number;
  soul: string;
  // The archetype whose base SOUL seeded the editor (ADR 0042) — "basic" by
  // default. Picking a card in the persona step seeds `soul` from it.
  archetype: string;
  middleware: AgentConfig["middleware"];
  researcherTurns: number;
  knowledgePath: string;
  knowledgeTopK: number;
  allowedDirs: string;
  initTasks: boolean;
};

function defaultState(): WizardState {
  return {
    agentName: "protoagent",
    operatorName: "",
    runtimeKind: "native",
    acpAgent: "proto",
    apiBase: "https://api.proto-labs.ai/v1",
    apiKey: "",
    modelName: "protolabs/reasoning",
    temperature: 0.2,
    maxTokens: 32768,
    maxIterations: 50,
    soul: "",
    archetype: "basic",
    middleware: {
      knowledge: true,
      audit: true,
      memory: true,
      scheduler: true,
    },
    researcherTurns: 40,
    knowledgePath: "",
    knowledgeTopK: 5,
    allowedDirs: "",
    initTasks: false,
  };
}

function hydrateState(payload: ConfigPayload): WizardState {
  const config = payload.config;
  const rt = String(config.agent_runtime || "native");
  return {
    agentName: config.identity.name || "protoagent",
    operatorName: config.identity.operator || "",
    runtimeKind: rt.startsWith("acp:") ? "acp" : "native",
    acpAgent: rt.startsWith("acp:") ? rt.slice(4) || "proto" : "proto",
    apiBase: config.model.api_base || "https://api.proto-labs.ai/v1",
    apiKey: "",
    modelName: config.model.name || "protolabs/reasoning",
    temperature: Number(config.model.temperature ?? 0.2),
    maxTokens: Number(config.model.max_tokens ?? 32768),
    maxIterations: Number(config.model.max_iterations ?? 50),
    // Start blank in the wizard (first-run-only flow): /api/config returns the
    // server's GENERIC default SOUL, not a user persona — the persona step seeds
    // the editor from the selected archetype instead. Leaving it blank lets that
    // seed run (a non-empty value here would suppress it).
    soul: "",
    archetype: "basic",
    middleware: {
      knowledge: Boolean(config.middleware.knowledge),
      audit: Boolean(config.middleware.audit),
      memory: Boolean(config.middleware.memory),
      scheduler: Boolean(config.middleware.scheduler),
    },
    researcherTurns: Number(config.subagents.researcher.max_turns ?? 40),
    knowledgePath: config.knowledge.db_path || "",
    knowledgeTopK: Number(config.knowledge.top_k ?? 5),
    allowedDirs: (config.operator?.allowed_dirs || []).join("\n"),
    initTasks: false,
  };
}

// A probe/test against a possibly-wrong or slow gateway must never hang the wizard —
// race it against a timeout so `busy` always clears and the step never locks (which
// would disable Next, trapping the user on the runtime step).
function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return Promise.race([
    p,
    new Promise<T>((_resolve, reject) =>
      setTimeout(
        () => reject(new Error(`${label} timed out after ${Math.round(ms / 1000)}s — check the API base and key`)),
        ms,
      ),
    ),
  ]);
}

export function SetupWizard({
  open,
  projectPath,
  onProjectPathChange,
  onFinished,
}: {
  open: boolean;
  projectPath: string;
  onProjectPathChange: (value: string) => void;
  onFinished: () => void;
}) {
  const [step, setStep] = useState<Step>("welcome");
  const [state, setState] = useState<WizardState>(() => defaultState());
  // Starter archetypes (Basic + Project Manager + installed bundles) — the same
  // source the fleet new-agent picker uses. Each carries a base SOUL the persona
  // step seeds when picked (ADR 0042).
  const archetypes = useQuery(archetypesQuery());
  const acpAgentList = useQuery(acpAgentsQuery()).data?.agents ?? [];
  const [models, setModels] = useState<string[]>([]);
  // Flips true once the initial config load finishes. The persona seed waits on it
  // so the async load() (which replaces the whole state) can't clobber the seed.
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  // Result of the last "Test connection" probe (a real completion). null = not
  // yet tested; invalidated whenever the key/base/model changes.
  const [tested, setTested] = useState<null | { ok: boolean; error: string }>(null);

  const index = steps.indexOf(step);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    async function load() {
      setBusy(true);
      setError("");
      try {
        const config = await api.config();
        if (!alive) return;
        setState(hydrateState(config));
        setLoaded(true);
      } catch (exc) {
        if (alive) setError(errMsg(exc));
      } finally {
        if (alive) setBusy(false);
      }
    }
    void load();
    return () => {
      alive = false;
    };
  }, [open]);

  // A changed key/base/model invalidates a prior connection test.
  useEffect(() => {
    setTested(null);
  }, [state.apiBase, state.apiKey, state.modelName]);

  const canGoNext = useMemo(() => {
    // On the brain step, a native model needs a gateway + model name. ACP needs
    // neither (the coding agent is the brain), so don't gate on the model fields.
    if (step === "brain")
      return Boolean(state.runtimeKind === "acp" || (state.apiBase.trim() && state.modelName.trim()));
    return true;
  }, [state.apiBase, state.modelName, state.runtimeKind, step]);

  function update(patch: Partial<WizardState>) {
    setState((current) => ({ ...current, ...patch }));
  }

  async function probeModels(opts?: { silent?: boolean }) {
    const silent = opts?.silent === true;
    if (!silent) setBusy(true);
    setError("");
    if (!silent) setModels([]);
    try {
      const response = await withTimeout(api.models(state.apiBase, state.apiKey), 15000, "Probe");
      if (response.error) {
        if (!silent) setError(response.error); // auto-probe stays quiet — the user may still be typing creds
        return;
      }
      setModels(response.models);
      // Only auto-select on an explicit probe; auto-probe must not clobber a
      // model the user (or a hydrated config) already chose.
      if (!silent && response.models.length && !response.models.includes(state.modelName)) {
        update({ modelName: response.models[0] });
      }
    } catch (exc) {
      if (!silent) setError(errMsg(exc));
    } finally {
      if (!silent) setBusy(false);
    }
  }

  // Auto-populate the model dropdown when the user reaches the model step with a
  // gateway base already filled (native runtime only — ACP needs no gateway), so
  // the picker is ready without a manual "Probe" click. Fires once per base;
  // silent so a not-yet-entered key doesn't flash an error. (bd-hbf)
  const autoProbedBase = useRef("");
  useEffect(() => {
    const base = state.apiBase.trim();
    if (step !== "brain" || state.runtimeKind !== "native" || !base) return;
    if (autoProbedBase.current === base || models.length > 0) return;
    autoProbedBase.current = base;
    void probeModels({ silent: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, state.runtimeKind, state.apiBase]);

  // The real auth check: a 1-token completion down the same path as chat, so a
  // bad key / wrong model is caught here in the UI rather than as a failed turn.
  async function testConnection() {
    setBusy(true);
    setError("");
    setTested(null);
    try {
      const r = await withTimeout(
        api.testModel(state.apiBase.trim(), state.apiKey.trim(), state.modelName.trim()),
        25000,
        "Test connection",
      );
      setTested({ ok: r.ok, error: r.error || "" });
    } catch (exc) {
      setTested({ ok: false, error: errMsg(exc) });
    } finally {
      setBusy(false);
    }
  }

  // Picking an archetype card seeds the editor with that archetype's base SOUL
  // — the same archetypes the fleet new-agent picker offers. The textarea stays
  // freely editable below; this is an explicit "load this persona" action.
  function pickArchetype(a: Archetype) {
    update({ archetype: a.id, soul: a.soul });
  }

  // Pre-fill the editor once with the default archetype's base SOUL so the persona
  // step isn't blank. Gated on `loaded` so it runs AFTER load() hydrates state —
  // otherwise load()'s full-state replace would clobber the seed and the guard would
  // block a retry, leaving it blank until the user toggles a card. Never clobbers an
  // in-session edit (soul already non-empty).
  const archetypeList = archetypes.data?.archetypes ?? [];
  const seededSoul = useRef(false);
  useEffect(() => {
    if (!loaded || seededSoul.current || !archetypeList.length || state.soul.trim()) return;
    const a = archetypeList.find((x) => x.id === state.archetype) ?? archetypeList[0];
    seededSoul.current = true;
    update({ archetype: a.id, soul: a.soul });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, archetypeList, state.soul, state.archetype]);

  // The picked archetype drives the finish summary AND (Plan C) the bundle install:
  // an archetype with a bundle (e.g. Project Manager → pm-stack) installs its plugins
  // into this host on finish, so the persona arrives WITH its tools.
  const pickedArchetype = archetypeList.find((a) => a.id === state.archetype);
  const personaLabel = pickedArchetype?.label ?? state.archetype;
  const pickedBundle = pickedArchetype?.bundle ?? null;
  const acpAgent = acpAgentList.find((a) => a.id === state.acpAgent);
  const acpAgentLabel = acpAgent?.label ?? state.acpAgent;
  const acpLaunchHint = acpAgent ? `${acpAgent.command} ${acpAgent.args.join(" ")}`.trim() : state.acpAgent;

  async function finishSetup() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const model: AgentConfig["model"] = {
        provider: "openai",
        name: state.modelName.trim(),
        api_base: state.apiBase.trim(),
        temperature: Number(state.temperature),
        max_tokens: Number(state.maxTokens),
        max_iterations: Number(state.maxIterations),
      };
      if (state.apiKey.trim()) {
        model.api_key = state.apiKey.trim();
      }
      // The project you're setting up should be operable, so fold its path
      // into the allowlist automatically — otherwise picking a project path
      // outside the (empty) allowlist silently makes tasks/notes unusable
      // for it. Extra dirs from the textarea are merged and de-duped.
      const allowedDirs = Array.from(
        new Set(
          [
            ...state.allowedDirs.split("\n").map((dir) => dir.trim()),
            projectPath.trim(),
          ].filter(Boolean),
        ),
      );
      const response = await api.finishSetup(
        {
          agent_runtime: state.runtimeKind === "acp" ? `acp:${state.acpAgent}` : "native",
          model,
          identity: {
            name: state.agentName.trim() || "protoagent",
            operator: state.operatorName.trim(),
          },
          middleware: state.middleware,
          subagents: {
            researcher: {
              enabled: true,
              tools: ["current_time", "web_search", "fetch_url", "memory_recall", "memory_list"],
              max_turns: Number(state.researcherTurns),
            },
          },
          knowledge: {
            db_path: state.knowledgePath.trim(),
            // Match the code default + what the protoLabs gateway serves. A model the
            // gateway can't access 401s every embed and silently degrades recall to
            // keyword-only — the Settings▸Knowledge field is a gateway-model dropdown.
            embed_model: "qwen3-embedding",
            top_k: Number(state.knowledgeTopK),
          },
          operator: {
            // The project dir is authoritative — the server's tasks/notes root
            // resolves to it (server._resolve_operator_project_root reads
            // operator.project_dir). Blank = the protoAgent dir. It's also folded
            // into allowed_dirs above so it's always operable.
            project_dir: projectPath.trim(),
            allowed_dirs: allowedDirs,
          },
          // Discord is managed in System → Settings, not the setup wizard. It's
          // omitted here so finishing setup leaves any existing integration config
          // untouched (the YAML write merges, never replaces).
        },
        state.soul,
      );
      if (!response.ok) {
        setError(response.message);
        return;
      }
      // The tasks store is agent-global and always ready, so this is a no-op
      // confirmation now (kept so the setup step still feels acknowledged).
      if (state.initTasks) {
        await api.initTasks();
      }
      // Plan C: if the chosen archetype carries a plugin bundle (e.g. Project
      // Manager → pm-stack), install it into THIS host on finish — so the new user
      // gets the persona AND its tools/board in one shot, not just the prose.
      // installPlugin auto-enables + hot-reloads the bundle's plugins (no restart).
      // A failure is non-fatal: setup is already written, so we finish anyway and
      // point the user at Settings ▸ Plugins.
      if (pickedBundle) {
        setMessage(`Setting up the ${personaLabel} tools — this can take a few seconds…`);
        try {
          const r = await api.installPlugin(pickedBundle);
          setMessage(
            r.enable_error
              ? `Setup complete. The ${personaLabel} tools installed but couldn't auto-enable (${r.enable_error}) — turn them on in Settings ▸ Plugins.`
              : `Setup complete — ${personaLabel} tools are ready.`,
          );
        } catch (exc) {
          setMessage(
            `Setup complete, but installing the ${personaLabel} tools failed (${errMsg(exc)}). You can add them later in Settings ▸ Plugins.`,
          );
        }
      } else {
        setMessage(response.message);
      }
      onFinished();
    } catch (exc) {
      setError(errMsg(exc));
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div className="setup-overlay" role="dialog" aria-modal="true" aria-label="Setup">
      <div className="setup-frame">
        <div className="setup-progress" aria-label="Setup progress">
          {steps.map((item, itemIndex) => (
            <span
              key={item}
              className={itemIndex < index ? "done" : itemIndex === index ? "active" : ""}
            />
          ))}
        </div>

        <section className="setup-card">
          {step === "welcome" ? (
            <StepBody icon={<Bot size={20} />} title="protoAgent" kicker="Welcome">
              <p className="setup-intro">
                A local-first AI agent that runs on your own machine. Your chats, files, and
                the agent&apos;s memory stay here — nothing leaves except the requests you send
                to the model gateway you choose.
              </p>
              <div className="setup-summary">
                <StatusLine icon={<HardDrive size={15} />} label="Local-first — runs on this machine" />
                <StatusLine icon={<ShieldCheck size={15} />} label="Private — you control its access" />
                <StatusLine icon={<KeyRound size={15} />} label="Bring your own model gateway" />
              </div>
              <p className="setup-intro setup-intro-foot">
                Takes a minute — everything here can be changed later in Settings.
              </p>
            </StepBody>
          ) : null}

          {step === "agent" ? (
            <StepBody icon={<Bot size={20} />} title="Agent" kicker="Name & persona">
              <div className="setup-grid two">
                <Field label="Agent name" value={state.agentName} onValueChange={(value) => update({ agentName: value })} />
                <Field label="Operator" value={state.operatorName} onValueChange={(value) => update({ operatorName: value })} />
              </div>
              <p className="setup-hint">
                Pick an archetype to seed the persona below — the agent&apos;s base SOUL. You can edit it freely.
              </p>
              <RadioCardGroup
                name="archetype"
                min="160px"
                value={state.archetype}
                onValueChange={(id) => {
                  const a = archetypeList.find((x) => x.id === id);
                  if (a) pickArchetype(a);
                }}
              >
                {archetypeList.map((a) => (
                  <RadioCard key={a.id} value={a.id} icon={lucideIcon(a.icon, 22)} title={a.label} blurb={a.blurb} />
                ))}
              </RadioCardGroup>
              <FormField label="SOUL.md">
                <Textarea className="setup-editor" value={state.soul} onChange={(event) => update({ soul: event.target.value })} />
              </FormField>
            </StepBody>
          ) : null}

          {step === "brain" ? (
            <StepBody
              icon={<Cpu size={20} />}
              title="Brain"
              kicker={state.runtimeKind === "acp" ? "coding agent over ACP" : "OpenAI-compatible gateway"}
            >
              {/* How this agent thinks: native LangGraph loop on a gateway, or hand each
                  turn to a CLI coding agent over ACP (ADR 0033). A radiogroup, so it gets a
                  plain label (not a FormField <label>, which should point at one control). */}
              <div className="pl-field">
                <span className="pl-field__label">How this agent thinks</span>
                <RadioCardGroup
                  name="runtime"
                  value={state.runtimeKind}
                  onValueChange={(kind) => update({ runtimeKind: kind as WizardState["runtimeKind"] })}
                >
                  <RadioCard value="native" title="Native model" blurb="Run turns on an OpenAI-compatible gateway." />
                  <RadioCard value="acp" title="Coding agent (ACP)" blurb="Hand each turn to a CLI coding agent — it's the brain, no gateway key needed." />
                </RadioCardGroup>
              </div>
              {state.runtimeKind === "acp" ? (
                <FormField
                  label="Coding agent"
                  hint={
                    <>
                      Launches <code>{acpLaunchHint}</code> — runtime set to{" "}
                      <code>acp:{state.acpAgent}</code>. No gateway key needed; a fallback model for native delegates can be set later in Settings.
                    </>
                  }
                >
                  <DropdownSelect
                    value={state.acpAgent}
                    onValueChange={(v) => update({ acpAgent: v })}
                    options={acpAgentList.map((a) => ({ value: a.id, label: a.label }))}
                  />
                </FormField>
              ) : (
                <>
                  {/* Native gateway config — only when the runtime is the native model.
                      ACP hands turns to the coding agent, so the gateway isn't shown there.
                      Temperature / max-tokens / max-turns are sensible defaults a new user
                      shouldn't have to tune — they flow through finishSetup; tweak later in
                      Settings. */}
                  <div className="setup-grid two">
                    <Field label="API base" value={state.apiBase} onValueChange={(value) => update({ apiBase: value })} />
                    <FormField label="API key">
                      <Input
                        type="password"
                        value={state.apiKey}
                        onChange={(event) => update({ apiKey: event.target.value })}
                        autoComplete="off"
                        placeholder="Leave blank to preserve current key"
                      />
                    </FormField>
                  </div>
                  <div className="setup-grid model-row">
                    <FormField label="Model">
                      <Input list="model-options" value={state.modelName} onChange={(event) => update({ modelName: event.target.value })} />
                      <datalist id="model-options">
                        {models.map((model) => (
                          <option key={model} value={model} />
                        ))}
                      </datalist>
                    </FormField>
                    <Button type="button" onClick={() => void probeModels()} disabled={busy || !state.apiBase.trim()}>
                      {busy ? <Spinner size={15} /> : <Search size={15} />}
                      Probe
                    </Button>
                    <TestConnectionButton
                      onClick={() => void testConnection()}
                      pending={busy}
                      disabled={!state.apiBase.trim() || !state.modelName.trim()}
                    />
                  </div>
                  {tested ? (
                    <Alert status={tested.ok ? "success" : "error"}>
                      {tested.ok
                        ? "Connection OK — the model responded."
                        : `Connection failed — ${tested.error || "the model did not respond."}`}
                    </Alert>
                  ) : null}
                </>
              )}
            </StepBody>
          ) : null}


          {step === "finish" ? (
            <StepBody icon={<Check size={20} />} title="You're all set" kicker="Review & finish">
              <div className="finish-list">
                <StatusLine icon={<Bot size={15} />} label={`Agent · ${state.agentName || "protoagent"}`} />
                {state.runtimeKind === "acp" ? (
                  <StatusLine icon={<KeyRound size={15} />} label={`Runtime · ${acpAgentLabel} (acp:${state.acpAgent})`} />
                ) : (
                  <StatusLine icon={<KeyRound size={15} />} label={`Model · ${state.modelName || "—"}`} />
                )}
                <StatusLine icon={<Sparkles size={15} />} label={`Persona · ${personaLabel}`} />
              </div>
              <p className="setup-intro setup-intro-foot">
                Finishing writes your config and starts the agent. Tools, knowledge, and
                integrations are all in Settings.
              </p>
              {message ? <Callout>{message}</Callout> : null}
            </StepBody>
          ) : null}

          {error ? <Callout tone="error">{error}</Callout> : null}

          <div className="setup-actions">
            <Button type="button" onClick={() => setStep(steps[Math.max(0, index - 1)])} disabled={index === 0 || busy}>
              <ChevronLeft size={15} />
              Back
            </Button>
            {step === "finish" ? (
              <Button variant="primary" type="button" onClick={() => void finishSetup()} disabled={busy}>
                {busy ? <Spinner size={15} /> : <Check size={15} />}
                Finish
              </Button>
            ) : (
              <Button variant="primary" type="button" onClick={() => setStep(steps[Math.min(steps.length - 1, index + 1)])} disabled={!canGoNext || busy}>
                Next
                <ChevronRight size={15} />
              </Button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function StepBody({
  icon,
  title,
  kicker,
  children,
}: {
  icon: ReactNode;
  title: string;
  kicker: string;
  children: ReactNode;
}) {
  return (
    <div className="setup-step">
      <div className="setup-heading">
        <div className="setup-icon">{icon}</div>
        <div>
          <h1>{title}</h1>
          <p>{kicker}</p>
        </div>
      </div>
      {children}
    </div>
  );
}

function StatusLine({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <div className="status-line">
      {icon}
      <span>{label}</span>
    </div>
  );
}
