import { Checkbox, Input, Select, Textarea } from "@protolabsai/ui/forms";
import { Button, Callout } from "@protolabsai/ui/primitives";
import {

  AlertTriangle,
  Bot,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  Database,
  ExternalLink,
  KeyRound,
  Loader2,
  MessageCircle,
  Network,
  Search,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../lib/api";
import type { AgentConfig, ConfigPayload, SetupStatus } from "../lib/types";

type Step = "welcome" | "identity" | "model" | "persona" | "tools" | "workspace" | "discord" | "google" | "finish";

const steps: Step[] = ["welcome", "identity", "model", "persona", "tools", "workspace", "discord", "google", "finish"];

// CLI coding agents that can be the runtime over ACP (ADR 0033) — agent_runtime: acp:<id>.
const ACP_AGENTS: { id: string; label: string; hint: string }[] = [
  { id: "proto", label: "proto", hint: "protoLabs CLI — proto --acp" },
  { id: "codex", label: "Codex", hint: "OpenAI Codex — npx @zed-industries/codex-acp" },
  { id: "claude", label: "Claude", hint: "Claude agent — npx @agentclientprotocol/claude-agent-acp" },
  { id: "opencode", label: "OpenCode", hint: "OpenCode — opencode acp" },
];

// Setup walkthroughs live in the template's (protoAgent) canonical docs — forks
// don't ship their own docs site, so the in-app help links point there.
const DISCORD_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/discord#bot-setup";
const GOOGLE_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/google#oauth-client";

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
  // Discord surface (optional, skippable). `discordAdminId` accepts one or more
  // IDs (comma/newline) — the operator's own user ID(s) allowed to DM the bot.
  discordEnabled: boolean;
  discordToken: string;
  discordAdminId: string;
  // Google surface (optional). Collect the OAuth client here; authorizing
  // ("Connect Google") happens in Settings after setup (it opens the browser).
  googleClientId: string;
  googleClientSecret: string;
  googleTz: string;
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
    knowledgePath: "/sandbox/knowledge/agent.db",
    knowledgeTopK: 5,
    allowedDirs: "",
    initBeads: false,
    discordEnabled: false,
    discordToken: "",
    discordAdminId: "",
    googleClientId: "",
    googleClientSecret: "",
    googleTz: "",
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
    knowledgePath: config.knowledge.db_path || "/sandbox/knowledge/agent.db",
    knowledgeTopK: Number(config.knowledge.top_k ?? 5),
    allowedDirs: (config.operator?.allowed_dirs || []).join("\n"),
    initBeads: false,
    discordEnabled: Boolean(config.discord?.enabled),
    discordToken: "",
    discordAdminId: (config.discord?.admin_ids || []).join(", "),
    googleClientId: config.google?.client_id || "",
    googleClientSecret: "",
    googleTz: config.google?.tz || "",
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
  // Discord "Test connection" result (null = not tested; carries the bot name).
  const [discordTested, setDiscordTested] = useState<null | { ok: boolean; error: string; bot: string | null }>(null);

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

  // A changed token invalidates a prior Discord test.
  useEffect(() => {
    setDiscordTested(null);
  }, [state.discordToken]);

  const canGoNext = useMemo(() => {
    // ACP runtime needs no gateway, so don't gate the step on the model fields.
    if (step === "model")
      return Boolean(state.runtimeKind === "acp" || (state.apiBase.trim() && state.modelName.trim()));
    if (step === "workspace") return state.knowledgePath.trim();
    return true;
  }, [state.apiBase, state.knowledgePath, state.modelName, state.runtimeKind, step]);

  function update(patch: Partial<WizardState>) {
    setState((current) => ({ ...current, ...patch }));
  }

  function setMiddleware(key: keyof WizardState["middleware"], value: boolean) {
    setState((current) => ({
      ...current,
      middleware: { ...current.middleware, [key]: value },
    }));
  }

  async function probeModels() {
    setBusy(true);
    setError("");
    setModels([]);
    try {
      const response = await api.models(state.apiBase, state.apiKey);
      if (response.error) {
        setError(response.error);
        return;
      }
      setModels(response.models);
      if (response.models.length && !response.models.includes(state.modelName)) {
        update({ modelName: response.models[0] });
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

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

  // Verify the Discord bot token before finishing — fetches the bot identity so
  // a bad token is caught in the UI (and the operator sees the bot's name).
  async function testDiscord() {
    setBusy(true);
    setError("");
    setDiscordTested(null);
    try {
      const r = await api.testDiscord(state.discordToken.trim());
      setDiscordTested({ ok: r.ok, error: r.error || "", bot: r.bot_user });
    } catch (exc) {
      setDiscordTested({ ok: false, error: exc instanceof Error ? exc.message : String(exc), bot: null });
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
            allowed_dirs: allowedDirs,
          },
          // Discord is enabled when a token is present (this run or already
          // stored). bot_token is only sent when entered — blank preserves the
          // existing secret, same as the model api_key.
          discord: {
            enabled: Boolean(state.discordToken.trim()) || state.discordEnabled,
            ...(state.discordToken.trim() ? { bot_token: state.discordToken.trim() } : {}),
            admin_ids: state.discordAdminId
              .split(/[\n,]/)
              .map((id) => id.trim())
              .filter(Boolean),
          },
          // Google: store the OAuth client; enabling happens, but the managed MCP
          // server only starts once "Connect Google" (Settings) mints a token.
          // client_secret only sent when entered (blank preserves the stored one).
          google: {
            enabled: Boolean(state.googleClientId.trim()),
            client_id: state.googleClientId.trim(),
            ...(state.googleClientSecret.trim() ? { client_secret: state.googleClientSecret.trim() } : {}),
            tz: state.googleTz.trim(),
          },
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
            <StepBody icon={<Database size={20} />} title="Workspace" kicker="Storage">
              <label className="field">
                <span>Knowledge DB</span>
                <Input value={state.knowledgePath} onChange={(event) => update({ knowledgePath: event.target.value })} />
              </label>
              <div className="setup-grid two">
                <label className="field">
                  <span>Knowledge top K</span>
                  <Input type="number" min="1" value={state.knowledgeTopK} onChange={(event) => update({ knowledgeTopK: Number(event.target.value) })} />
                </label>
                <label className="field">
                  <span>Project path</span>
                  <Input value={projectPath} onChange={(event) => onProjectPathChange(event.target.value)} />
                </label>
              </div>
              <label className="field">
                <span>Allowed project directories</span>
                <Textarea
                  rows={3}
                  value={state.allowedDirs}
                  onChange={(event) => update({ allowedDirs: event.target.value })}
                  placeholder={"One path per line.\nThe protoAgent directory is always allowed."}
                />
                <span className="field-hint">
                  Beads and notes may only read/write inside these directories. One per line.
                  The protoAgent directory and the project path above are always allowed.
                </span>
              </label>
              <Checkbox
                className="checkbox-field setup-checkbox"
                checked={state.initBeads}
                onCheckedChange={(c) => update({ initBeads: c })}
                label="Initialize beads"
              />
            </StepBody>
          ) : null}

          {step === "discord" ? (
            <StepBody icon={<MessageCircle size={20} />} title="Discord" kicker="Optional">
              <p className="field-hint" style={{ marginTop: 0 }}>
                Connect a Discord bot so you can DM {state.agentName || "your agent"} from
                Discord. Optional — skip it and set it up later in System → Settings.{" "}
                <a href={DISCORD_GUIDE_URL} target="_blank" rel="noreferrer" className="setup-link">
                  How to create a bot <ExternalLink size={12} />
                </a>
              </p>
              <label className="field">
                <span>Bot token</span>
                <Input
                  type="password"
                  value={state.discordToken}
                  onChange={(event) => update({ discordToken: event.target.value })}
                  autoComplete="off"
                  placeholder="Leave blank to skip Discord"
                />
              </label>
              <div className="setup-grid model-row">
                <label className="field">
                  <span>Your Discord user ID(s)</span>
                  <Input
                    value={state.discordAdminId}
                    onChange={(event) => update({ discordAdminId: event.target.value })}
                    placeholder="e.g. 249386616806834177 — comma-separated; empty = anyone"
                  />
                </label>
                <Button
                 
                  type="button"
                  onClick={() => void testDiscord()}
                  disabled={busy || !state.discordToken.trim()}
                >
                  {busy ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </Button>
              </div>
              {discordTested ? (
                <div className={`setup-test ${discordTested.ok ? "ok" : "err"}`} role="status">
                  {discordTested.ok ? <Check size={15} /> : <AlertTriangle size={15} />}
                  <span>
                    {discordTested.ok
                      ? `Connected as ${discordTested.bot || "your bot"}.`
                      : `Connection failed — ${discordTested.error || "check the token."}`}
                  </span>
                </div>
              ) : null}
            </StepBody>
          ) : null}

          {step === "google" ? (
            <StepBody icon={<CalendarDays size={20} />} title="Google" kicker="Optional">
              <p className="field-hint" style={{ marginTop: 0 }}>
                Give {state.agentName || "your agent"} read access to Gmail + Calendar. Paste your
                Google Cloud <strong>Desktop app</strong> OAuth client below — after setup, click
                <strong> Connect Google</strong> in System → Settings to authorize (it opens your
                browser). Optional — skip to do it later.{" "}
                <a href={GOOGLE_GUIDE_URL} target="_blank" rel="noreferrer" className="setup-link">
                  Get an OAuth client <ExternalLink size={12} />
                </a>
              </p>
              <div className="setup-grid two">
                <label className="field">
                  <span>OAuth client ID</span>
                  <Input
                    value={state.googleClientId}
                    onChange={(event) => update({ googleClientId: event.target.value })}
                    placeholder="…apps.googleusercontent.com"
                  />
                </label>
                <label className="field">
                  <span>OAuth client secret</span>
                  <Input
                    type="password"
                    value={state.googleClientSecret}
                    onChange={(event) => update({ googleClientSecret: event.target.value })}
                    autoComplete="off"
                    placeholder="Leave blank to skip Google"
                  />
                </label>
              </div>
              <label className="field">
                <span>Timezone (IANA, optional)</span>
                <Input
                  value={state.googleTz}
                  onChange={(event) => update({ googleTz: event.target.value })}
                  placeholder="e.g. America/Los_Angeles — sets the day bounds for “today”"
                />
              </label>
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
