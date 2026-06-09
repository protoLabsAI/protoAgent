import { Button } from "@protolabsai/ui/primitives";
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

// Setup walkthroughs live in the template's (protoAgent) canonical docs — forks
// don't ship their own docs site, so the in-app help links point there.
const DISCORD_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/discord#bot-setup";
const GOOGLE_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/google#oauth-client";

type WizardState = {
  agentName: string;
  operatorName: string;
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
  return {
    agentName: config.identity.name || "protoagent",
    operatorName: config.identity.operator || "",
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
    if (step === "model") return state.apiBase.trim() && state.modelName.trim();
    if (step === "workspace") return state.knowledgePath.trim();
    return true;
  }, [state.apiBase, state.knowledgePath, state.modelName, step]);

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
            embed_model: "nomic-embed-text",
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
                  <input value={state.agentName} onChange={(event) => update({ agentName: event.target.value })} />
                </label>
                <label className="field">
                  <span>Operator</span>
                  <input value={state.operatorName} onChange={(event) => update({ operatorName: event.target.value })} />
                </label>
              </div>
            </StepBody>
          ) : null}

          {step === "model" ? (
            <StepBody icon={<KeyRound size={20} />} title="Model Gateway" kicker="OpenAI-compatible">
              <div className="setup-grid two">
                <label className="field">
                  <span>API base</span>
                  <input value={state.apiBase} onChange={(event) => update({ apiBase: event.target.value })} />
                </label>
                <label className="field">
                  <span>API key</span>
                  <input
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
                  <input list="model-options" value={state.modelName} onChange={(event) => update({ modelName: event.target.value })} />
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
                  <input type="number" min="0" max="2" step="0.1" value={state.temperature} onChange={(event) => update({ temperature: Number(event.target.value) })} />
                </label>
                <label className="field">
                  <span>Max tokens</span>
                  <input type="number" min="1" value={state.maxTokens} onChange={(event) => update({ maxTokens: Number(event.target.value) })} />
                </label>
                <label className="field">
                  <span>Max turns</span>
                  <input type="number" min="1" value={state.maxIterations} onChange={(event) => update({ maxIterations: Number(event.target.value) })} />
                </label>
              </div>
            </StepBody>
          ) : null}

          {step === "persona" ? (
            <StepBody icon={<Sparkles size={20} />} title="SOUL" kicker="Persona">
              <div className="setup-grid model-row">
                <label className="field">
                  <span>Preset</span>
                  <select value={state.preset} onChange={(event) => update({ preset: event.target.value })}>
                    {(setupStatus?.presets || []).map((preset) => (
                      <option key={preset} value={preset}>
                        {preset}
                      </option>
                    ))}
                  </select>
                </label>
                <Button type="button" onClick={() => void loadPreset()} disabled={busy || !state.preset}>
                  {busy ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}
                  Load
                </Button>
              </div>
              <label className="field">
                <span>SOUL.md</span>
                <textarea className="setup-editor" value={state.soul} onChange={(event) => update({ soul: event.target.value })} />
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
                <input type="number" min="1" value={state.researcherTurns} onChange={(event) => update({ researcherTurns: Number(event.target.value) })} />
              </label>
            </StepBody>
          ) : null}

          {step === "workspace" ? (
            <StepBody icon={<Database size={20} />} title="Workspace" kicker="Storage">
              <label className="field">
                <span>Knowledge DB</span>
                <input value={state.knowledgePath} onChange={(event) => update({ knowledgePath: event.target.value })} />
              </label>
              <div className="setup-grid two">
                <label className="field">
                  <span>Knowledge top K</span>
                  <input type="number" min="1" value={state.knowledgeTopK} onChange={(event) => update({ knowledgeTopK: Number(event.target.value) })} />
                </label>
                <label className="field">
                  <span>Project path</span>
                  <input value={projectPath} onChange={(event) => onProjectPathChange(event.target.value)} />
                </label>
              </div>
              <label className="field">
                <span>Allowed project directories</span>
                <textarea
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
              <label className="checkbox-field setup-checkbox">
                <input type="checkbox" checked={state.initBeads} onChange={(event) => update({ initBeads: event.target.checked })} />
                <span>Initialize beads</span>
              </label>
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
                <input
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
                  <input
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
                  <input
                    value={state.googleClientId}
                    onChange={(event) => update({ googleClientId: event.target.value })}
                    placeholder="…apps.googleusercontent.com"
                  />
                </label>
                <label className="field">
                  <span>OAuth client secret</span>
                  <input
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
                <input
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
              {message ? <div className="setup-message">{message}</div> : null}
            </StepBody>
          ) : null}

          {error ? <div className="setup-error">{error}</div> : null}

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
