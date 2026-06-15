import { Checkbox, Input, Select, Textarea } from "@protolabsai/ui/forms";
import { Button, Callout } from "@protolabsai/ui/primitives";
import {

  AlertTriangle,
  Bot,
  Check,
  ChevronLeft,
  ChevronRight,
  Database,
  KeyRound,
  Loader2,
  Network,
  Search,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../lib/api";
import type { AgentConfig, ConfigPayload, SetupStatus } from "../lib/types";

type Step = "welcome" | "identity" | "model" | "persona" | "tools" | "workspace" | "finish";

const steps: Step[] = ["welcome", "identity", "model", "persona", "tools", "workspace", "finish"];

// CLI coding agents that can be the runtime over ACP (ADR 0033) — agent_runtime: acp:<id>.
const ACP_AGENTS: { id: string; label: string; hint: string }[] = [
  { id: "proto", label: "proto", hint: "protoLabs CLI — proto --acp" },
  { id: "codex", label: "Codex", hint: "OpenAI Codex — npx @zed-industries/codex-acp" },
  { id: "claude", label: "Claude", hint: "Claude agent — npx @agentclientprotocol/claude-agent-acp" },
  { id: "copilot", label: "Copilot", hint: "GitHub Copilot CLI — copilot --acp" },
  { id: "opencode", label: "OpenCode", hint: "OpenCode — opencode acp" },
];

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
  preset: string;
  middleware: AgentConfig["middleware"];
  researcherTurns: number;
  knowledgePath: string;
  knowledgeTopK: number;
  allowedDirs: string;
  initBeads: boolean;
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
    preset: "",
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
    initBeads: false,
  };
}

function hydrateState(payload: ConfigPayload, status: SetupStatus | null): WizardState {
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
    soul: payload.soul || "",
    preset: status?.presets[0] || "",
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
    initBeads: false,
  };
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
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  const [models, setModels] = useState<string[]>([]);
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
        const [status, config] = await Promise.all([api.setupStatus(), api.config()]);
        if (!alive) return;
        setSetupStatus(status);
        setState(hydrateState(config, status));
      } catch (exc) {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc));
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
    // ACP runtime needs no gateway, so don't gate the step on the model fields.
    if (step === "model")
      return Boolean(state.runtimeKind === "acp" || (state.apiBase.trim() && state.modelName.trim()));
    // The workspace step has no required fields — Knowledge DB is optional (blank
    // = the default location) and the project dir defaults to the protoAgent dir.
    return true;
  }, [state.apiBase, state.modelName, state.runtimeKind, step]);

  function update(patch: Partial<WizardState>) {
    setState((current) => ({ ...current, ...patch }));
  }

  function setMiddleware(key: keyof WizardState["middleware"], value: boolean) {
    setState((current) => ({
      ...current,
      middleware: { ...current.middleware, [key]: value },
    }));
  }

  async function probeModels(opts?: { silent?: boolean }) {
    const silent = opts?.silent === true;
    if (!silent) setBusy(true);
    setError("");
    if (!silent) setModels([]);
    try {
      const response = await api.models(state.apiBase, state.apiKey);
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
      if (!silent) setError(exc instanceof Error ? exc.message : String(exc));
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
    if (step !== "model" || state.runtimeKind !== "native" || !base) return;
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
      const r = await api.testModel(state.apiBase.trim(), state.apiKey.trim(), state.modelName.trim());
      setTested({ ok: r.ok, error: r.error || "" });
    } catch (exc) {
      setTested({ ok: false, error: exc instanceof Error ? exc.message : String(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function loadPreset() {
    if (!state.preset) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.soulPreset(state.preset);
      update({ soul: response.content });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

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
      // outside the (empty) allowlist silently makes beads/notes unusable
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
            // The project dir is authoritative — the server's beads/notes root
            // resolves to it (server._resolve_operator_project_root reads
            // operator.project_dir). Blank = the protoAgent dir. It's also folded
            // into allowed_dirs above so it's always operable.
            project_dir: projectPath.trim(),
            allowed_dirs: allowedDirs,
          },
          // Discord + Google are managed in System → Settings, not the setup
          // wizard. They're omitted here so finishing setup leaves any existing
          // integration config untouched (the YAML write merges, never replaces).
        },
        state.soul,
      );
      if (!response.ok) {
        setError(response.message);
        return;
      }
      // The beads store is agent-global and always ready, so this is a no-op
      // confirmation now (kept so the setup step still feels acknowledged).
      if (state.initBeads) {
        await api.initBeads();
      }
      setMessage(response.message);
      onFinished();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
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
            <StepBody icon={<Bot size={20} />} title="protoAgent" kicker="Setup">
              <div className="setup-summary">
                <StatusLine icon={<KeyRound size={15} />} label="Model gateway" />
                <StatusLine icon={<Sparkles size={15} />} label="SOUL profile" />
                <StatusLine icon={<Database size={15} />} label="Workspace" />
                <StatusLine icon={<Network size={15} />} label="Subagents" />
              </div>
            </StepBody>
          ) : null}

          {step === "identity" ? (
            <StepBody icon={<Bot size={20} />} title="Identity" kicker="Agent">
              <div className="setup-grid two">
                <label className="field">
                  <span>Agent name</span>
                  <Input value={state.agentName} onChange={(event) => update({ agentName: event.target.value })} />
                </label>
                <label className="field">
                  <span>Operator</span>
                  <Input value={state.operatorName} onChange={(event) => update({ operatorName: event.target.value })} />
                </label>
              </div>
            </StepBody>
          ) : null}

          {step === "model" ? (
            <StepBody
              icon={<KeyRound size={20} />}
              title="Runtime"
              kicker={state.runtimeKind === "acp" ? "coding agent over ACP" : "OpenAI-compatible gateway"}
            >
              {/* How this agent thinks: native LangGraph loop on a gateway, or hand each
                  turn to a CLI coding agent over ACP (ADR 0033). */}
              <label className="field">
                <span>How this agent thinks</span>
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  {([
                    ["native", "Native model", "Run turns on an OpenAI-compatible gateway."],
                    ["acp", "Coding agent (ACP)", "Hand each turn to a CLI coding agent — it's the brain, no gateway key needed."],
                  ] as const).map(([kind, label, blurb]) => (
                    <button
                      key={kind}
                      type="button"
                      onClick={() => update({ runtimeKind: kind })}
                      style={{
                        flex: 1,
                        textAlign: "left",
                        padding: "0.6rem 0.7rem",
                        borderRadius: 8,
                        cursor: "pointer",
                        color: "inherit",
                        border: `1px solid ${state.runtimeKind === kind ? "var(--brand-violet-light, #a78bfa)" : "var(--border, #333)"}`,
                        background:
                          state.runtimeKind === kind
                            ? "color-mix(in srgb, var(--brand-violet-light, #a78bfa) 14%, transparent)"
                            : "transparent",
                      }}
                    >
                      <strong style={{ display: "block", fontSize: 13 }}>{label}</strong>
                      <small style={{ opacity: 0.7 }}>{blurb}</small>
                    </button>
                  ))}
                </div>
              </label>
              {state.runtimeKind === "acp" ? (
                <label className="field">
                  <span>Coding agent</span>
                  <select
                    value={state.acpAgent}
                    onChange={(event) => update({ acpAgent: event.target.value })}
                    style={{
                      padding: "0.5rem",
                      borderRadius: 8,
                      color: "inherit",
                      background: "var(--bg-panel, #1a1a1a)",
                      border: "1px solid var(--border, #333)",
                    }}
                  >
                    {ACP_AGENTS.map((a) => (
                      <option key={a.id} value={a.id}>{a.label}</option>
                    ))}
                  </select>
                  <small style={{ opacity: 0.7 }}>
                    {ACP_AGENTS.find((a) => a.id === state.acpAgent)?.hint} — runtime set to{" "}
                    <code>acp:{state.acpAgent}</code>. The gateway below is <strong>optional</strong> (only for native delegates / fallback).
                  </small>
                </label>
              ) : null}
              <div className="setup-grid two">
                <label className="field">
                  <span>API base</span>
                  <Input value={state.apiBase} onChange={(event) => update({ apiBase: event.target.value })} />
                </label>
                <label className="field">
                  <span>API key</span>
                  <Input
                    type="password"
                    value={state.apiKey}
                    onChange={(event) => update({ apiKey: event.target.value })}
                    autoComplete="off"
                    placeholder="Leave blank to preserve current key"
                  />
                </label>
              </div>
              <div className="setup-grid model-row">
                <label className="field">
                  <span>Model</span>
                  <Input list="model-options" value={state.modelName} onChange={(event) => update({ modelName: event.target.value })} />
                  <datalist id="model-options">
                    {models.map((model) => (
                      <option key={model} value={model} />
                    ))}
                  </datalist>
                </label>
                <Button type="button" onClick={() => void probeModels()} disabled={busy || !state.apiBase.trim()}>
                  {busy ? <Loader2 className="spin" size={15} /> : <Search size={15} />}
                  Probe
                </Button>
                <Button
                 
                  type="button"
                  onClick={() => void testConnection()}
                  disabled={busy || !state.apiBase.trim() || !state.modelName.trim()}
                >
                  {busy ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </Button>
              </div>
              {tested ? (
                <div className={`setup-test ${tested.ok ? "ok" : "err"}`} role="status">
                  {tested.ok ? <Check size={15} /> : <AlertTriangle size={15} />}
                  <span>
                    {tested.ok
                      ? "Connection OK — the model responded."
                      : `Connection failed — ${tested.error || "the model did not respond."}`}
                  </span>
                </div>
              ) : null}
              <div className="setup-grid three">
                <label className="field">
                  <span>Temperature</span>
                  <Input type="number" min="0" max="2" step="0.1" value={state.temperature} onChange={(event) => update({ temperature: Number(event.target.value) })} />
                </label>
                <label className="field">
                  <span>Max tokens</span>
                  <Input type="number" min="1" value={state.maxTokens} onChange={(event) => update({ maxTokens: Number(event.target.value) })} />
                </label>
                <label className="field">
                  <span>Max turns</span>
                  <Input type="number" min="1" value={state.maxIterations} onChange={(event) => update({ maxIterations: Number(event.target.value) })} />
                </label>
              </div>
            </StepBody>
          ) : null}

          {step === "persona" ? (
            <StepBody icon={<Sparkles size={20} />} title="SOUL" kicker="Persona">
              <div className="setup-grid model-row">
                <label className="field">
                  <span>Preset</span>
                  <Select value={state.preset} onChange={(event) => update({ preset: event.target.value })}>
                    {(setupStatus?.presets || []).map((preset) => (
                      <option key={preset} value={preset}>
                        {preset}
                      </option>
                    ))}
                  </Select>
                </label>
                <Button type="button" onClick={() => void loadPreset()} disabled={busy || !state.preset}>
                  {busy ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}
                  Load
                </Button>
              </div>
              <label className="field">
                <span>SOUL.md</span>
                <Textarea className="setup-editor" value={state.soul} onChange={(event) => update({ soul: event.target.value })} />
              </label>
            </StepBody>
          ) : null}

          {step === "tools" ? (
            <StepBody icon={<ShieldCheck size={20} />} title="Tools" kicker="Middleware">
              <div className="toggle-grid">
                {Object.entries(state.middleware).map(([key, value]) => (
                  <label className="toggle-row" key={key}>
                    <span>{key}</span>
                    <input
                      type="checkbox"
                      checked={value}
                      onChange={(event) => setMiddleware(key as keyof WizardState["middleware"], event.target.checked)}
                    />
                  </label>
                ))}
              </div>
              <label className="field">
                <span>Researcher turns</span>
                <Input type="number" min="1" value={state.researcherTurns} onChange={(event) => update({ researcherTurns: Number(event.target.value) })} />
              </label>
            </StepBody>
          ) : null}

          {step === "workspace" ? (
            <StepBody icon={<Database size={20} />} title="Workspace" kicker="Project & memory">
              {/* Group 1 — Project: where the agent works (the relatable concept). */}
              <span className="field-hint">
                <strong>Project</strong> — the directory this agent works in.
              </span>
              <label className="field">
                <span>Project directory</span>
                <Input
                  value={projectPath}
                  onChange={(event) => onProjectPathChange(event.target.value)}
                  placeholder="Absolute path — defaults to the protoAgent directory"
                />
                <span className="field-hint">
                  Where this agent's beads &amp; notes live, and its default project. Must already
                  exist. Leave blank to use the protoAgent directory.
                  {projectPath.trim() && !projectPath.trim().startsWith("/") && !projectPath.trim().startsWith("~") ? (
                    <span className="field-warn"> Use an absolute path (starting with / or ~).</span>
                  ) : null}
                </span>
              </label>
              <label className="field">
                <span>Additional allowed directories</span>
                <Textarea
                  rows={2}
                  value={state.allowedDirs}
                  onChange={(event) => update({ allowedDirs: event.target.value })}
                  placeholder={"One absolute path per line — usually left blank."}
                />
                <span className="field-hint">
                  Extra directories beads &amp; notes may read/write, beyond the project directory
                  and protoAgent (which are always allowed). One per line. Most setups leave this blank.
                </span>
              </label>

              {/* Group 2 — Memory: the RAG knowledge store. Advanced; safe defaults. */}
              <span className="field-hint">
                <strong>Memory</strong> — the long-term knowledge store (RAG). Defaults are fine.
              </span>
              <div className="setup-grid two">
                <label className="field">
                  <span>Knowledge database</span>
                  <Input
                    value={state.knowledgePath}
                    onChange={(event) => update({ knowledgePath: event.target.value })}
                    placeholder="Leave blank for the default"
                  />
                  <span className="field-hint">Blank = the default location (~/.protoagent/knowledge).</span>
                </label>
                <label className="field">
                  <span>Recall results (top-K)</span>
                  <Input type="number" min="1" value={state.knowledgeTopK} onChange={(event) => update({ knowledgeTopK: Number(event.target.value) })} />
                  <span className="field-hint">How many memory snippets each recall returns.</span>
                </label>
              </div>
              <Checkbox
                className="checkbox-field setup-checkbox"
                checked={state.initBeads}
                onCheckedChange={(c) => update({ initBeads: c })}
                label="Initialize beads (task tracker) in the project directory"
              />
            </StepBody>
          ) : null}

          {step === "finish" ? (
            <StepBody icon={<Check size={20} />} title="Finish" kicker="Write config">
              <div className="finish-list">
                <StatusLine icon={<Bot size={15} />} label={state.agentName || "protoagent"} />
                <StatusLine icon={<KeyRound size={15} />} label={state.modelName || "model"} />
                <StatusLine icon={<Database size={15} />} label={state.knowledgePath || "knowledge"} />
                <StatusLine icon={<Network size={15} />} label={`${state.researcherTurns} researcher turns`} />
              </div>
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
                {busy ? <Loader2 className="spin" size={15} /> : <Check size={15} />}
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
