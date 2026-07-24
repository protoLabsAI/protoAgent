export type RuntimeStatus = {
  setup_complete: boolean;
  graph_loaded: boolean;
  /** App version (pyproject [project].version; the frozen desktop sidecar reports
   *  its bundled version — #894). Surfaced in Settings ▸ Global ▸ Overview. */
  version?: string;
  project: {
    path: string;
    allowed_dirs?: string[];
  };
  /** Which brain drives a turn (ADR 0033): "native" = the LangGraph loop, or
   *  "acp:<agent>" = an external coding agent. The console reads this to label the
   *  active runtime instead of the gateway model and to flag that protoAgent
   *  skills/commands don't apply in coding-agent mode. */
  agent_runtime?: string;
  model: null | {
    provider: string;
    name: string;
    api_base: string;
    api_key_configured: boolean;
    temperature: number | null;
    max_tokens: number | null;
    max_iterations: number | null;
    /** Model accepts native image input (model.vision) — chat sends attached
     *  images straight to the model instead of through the extraction pipeline. */
    vision?: boolean;
    /** A vision model is configured to describe images for a text-only chat model
     *  (knowledge.image_describe_model, #1381) — images route through the describe
     *  pipeline instead of erroring. */
    image_describe?: boolean;
  };
  identity: null | {
    name: string;
    operator: string;
    org?: string;
  };
  middleware: Record<string, boolean>;
  knowledge: {
    enabled: boolean;
    // "ready" (store built) · "initializing" (flag on, store still warming up during
    // boot/recompile) · "disabled" (flag off). Optional for older backends.
    status?: "ready" | "initializing" | "disabled";
    configured_path: string | null;
    resolved_path: string | null;
    top_k?: number | null;
  };
  scheduler: {
    enabled: boolean;
    backend: string;
  };
  cache_warmer: {
    enabled: boolean;
    loaded: boolean;
    interval_seconds?: number | null;
  };
  storage?: {
    knowledge_bytes?: number | null;
    telemetry_bytes?: number | null;
    checkpoint_bytes?: number | null;
    skills_bytes?: number | null;
    telemetry_retention_days?: number | null;
  };
  /** User-facing operational alerts (e.g. a live co-located instance sharing
   *  this data root, #706) — the shell banners them under the topbar. */
  warnings?: string[];
  /** Stable per-data-root uid — the TenantGuard keys per-origin client state on it
   *  (a different backend reusing this address must not render this one's chats). */
  instance_uid?: string;
  skills?: {
    enabled: boolean;
    count: number;
    top_k?: number | null;
  };
  mcp?: {
    enabled: boolean;
    // tier (ADR 0041): "commons" (box-shared) · "private" (this agent) · "managed"
    // (plugin-contributed) · null. Present only when the agent is layered; drives the
    // console's tier badge + share/unshare.
    servers: { name: string; transport: string; tool_count: number; tier?: "commons" | "private" | "managed" | null }[];
    tool_count: number;
  };
  plugins?: {
    id: string;
    name: string;
    version?: string;
    enabled: boolean;
    loaded: boolean;
    // Core runtime infrastructure (e.g. the delegate registry): always loaded, can't
    // be disabled, and hidden from the Plugins management list (its config lives in the
    // core Workspace settings, not the Plugins panel).
    builtin?: boolean;
    tools: string[];
    skills: number;
    error?: string;
    // Required-config gate (#1719): the plugin loaded but a `required: true` setting
    // is still blank — its tools return a "needs setup" notice until it's configured.
    incomplete?: boolean;
    needs_config?: { key: string; label: string }[];
    // Console surfaces (ADR 0026): rail views the plugin contributes.
    views?: PluginView[];
  }[];
};

// A plugin-contributed console surface (ADR 0026): a rail icon opening an iframe
// of `path` (served by the plugin), with optional sub-tabs.
export type PluginView = {
  id: string;
  label: string;
  icon?: string; // a lucide-react icon name
  path: string;
  // The console surface key (`plugin:<pluginId>:<viewId>`) — stamped on by App when it
  // builds the view list from runtime status. The view host needs it to report per-view
  // state upward (a `background: true` subscribe → the ui store, #1640). Optional:
  // absent on raw manifest-shaped views that never passed through App's mapping.
  key?: string;
  tabs?: { id: string; label: string; path: string }[];
  // "rail" (default) = a left-rail surface; "right" = a right-sidebar panel
  // alongside Notes/Tasks/Goals/Schedule (ADR 0026); "bottom" = the bottom dock.
  placement?: "rail" | "right" | "bottom";
  // Claim a core surface slot instead of adding a rail icon (ADR 0045). A view with
  // slot:"chat" REPLACES the built-in chat panel — it renders under the core "chat"
  // rail id, stays mounted for the app's lifetime (streaming continuity, #613), and
  // does not get its own rail entry. First enabled claimant wins.
  slot?: "chat";
  // ADR 0057 — opt this view into the command palette as an INLINE morph target: its
  // ⌘K command expands the plugin's iframe in the palette body (themed/authed via the
  // same handshake) instead of navigating to its rail. `"inline"` reuses this view's
  // `path`; `{ path }` ships a DISTINCT page for the palette (e.g. a tighter quick
  // editor) vs the full rail panel — so a plugin can ship separate panel/palette views.
  // Passes through `_parse_views` verbatim (it keeps the whole view dict) — manifest-only.
  palette?: "inline" | { path?: string };
  // Render this view as a UTILITY-BAR WIDGET (2026-06 IA pass): a bottom-left pill that
  // shows a hover info popover and opens the view's iframe in a DIALOG on click, instead of
  // adding a rail surface. `true` uses the label as the hover info; `{ info }` sets custom
  // hover text. Like `palette`, it rides through `_parse_views` verbatim — manifest-only.
  utility?: boolean | { info?: string };
  // Owning plugin's load state, stamped on by App so the view host can surface a real,
  // actionable error (loaded=false ⇒ the view route isn't serving — missing env / bad
  // deps / mount race; `pluginError` is the loader's exact diagnostic) instead of a
  // blank panel. Optional: present only for runtime-status-sourced views.
  pluginLoaded?: boolean;
  pluginError?: string;
};

// A git-installed plugin (ADR 0027) — a plugins.lock entry enriched with its
// manifest + enabled state for the console Plugins panel.
export type InstalledPlugin = {
  id: string;
  source_url: string;
  requested_ref: string;
  resolved_sha: string;
  installed_at?: string;
  by?: string;
  present: boolean;
  // false = on disk but not in plugins.lock (a local/dev copy) — no source_url/SHA,
  // so it can't be update-checked or re-synced. Disk is the source of truth, so it's
  // still listed (and enabled/loaded normally); it just isn't update-tracked.
  tracked?: boolean;
  enabled: boolean;
  // Required-config gate (#1719) — merged from the loader meta: true when the plugin
  // loaded but a `required: true` setting is blank, with the fields still needed.
  incomplete?: boolean;
  needs_config?: { key: string; label: string }[];
  // Declared requires_pip entries missing from the runtime — drives the one-click
  // "Install deps" action (POST /api/plugins/install-deps).
  deps_missing?: string[];
  manifest?: {
    name: string;
    version: string;
    description: string;
    repository?: string;
    homepage?: string;
    capabilities?: Record<string, unknown>;
    requires_env?: string[];
    requires_pip?: string[];
    views?: string[];
    secrets?: string[];
  };
};

// A fenced filesystem root (ADR 0007) — one entry of `filesystem.projects`.
export type FsProject = { name?: string; path: string; write: boolean };

// An entry in the official-plugin directory (GET /api/plugins/catalog, ADR 0059),
// merged with install state. `repo` is the install URL — one-click install runs
// `plugin install <repo>`. `bundled` = a built-in still shipped with the host (not
// separately installable); `installed` = present in the live plugins dir.
export type CatalogPlugin = {
  id: string;
  name: string;
  tagline?: string;
  category?: string;
  official?: boolean;
  repo: string;
  bundled: boolean;
  installed: boolean;
  enabled: boolean;
};

// A value the operator must supply before a catalog server is added — a filesystem
// path, an API token. `secret` renders a password field; the value is substituted
// into the `${key}` placeholders in the entry's template.
export type McpCatalogInput = {
  key: string;
  label: string;
  placeholder?: string;
  secret?: boolean;
  required?: boolean;
};

// An entry in the curated common-MCP-servers directory (GET /api/mcp/catalog).
// `template` is a partial mcp.servers config with `${input}` placeholders; filling
// the `inputs` and substituting yields the entry POSTed to /api/mcp/servers.
// `installed` = a server with this name is already configured.
export type McpCatalogEntry = {
  id: string;
  name: string;
  category?: string;
  tagline?: string;
  docs?: string;
  requires?: string;
  official?: boolean;
  template: Record<string, unknown>;
  inputs?: McpCatalogInput[];
  installed?: boolean;
};

// Per-plugin update status (GET /api/plugins/updates, ADR 0027). The backend
// TTL-caches these — a *pinned* plugin (its requested_ref is a full SHA) skips
// the network; the rest ls-remote their ref. `behind` ⇒ the recorded
// `current_sha` lags `latest_sha`; a per-entry `error` is non-fatal (the check
// failed, the row stays usable). `latest_sha` is null when pinned or on error.
// `latest_ref` is set when a release-tag pin has a NEWER semver tag to move to
// (the update installs that tag); null for branch refs / moved-tag cases.
export type PluginUpdate = {
  id: string;
  behind: boolean;
  pinned: boolean;
  current_sha: string;
  latest_sha: string | null;
  latest_ref?: string | null;
  error?: string | null;
};

// The summary returned right after installing (the review card).
export type PluginInstallSummary = {
  id: string;
  name: string;
  version: string;
  description: string;
  resolved_sha: string;
  source_url: string;
  requires_pip: string[];
  capabilities: Record<string, unknown>;
  contributes: { views: string[]; secrets: string[] };
};

export type SlashCommand = {
  name: string;
  description: string;
  usage?: string;
  // What the token dispatches to (server-resolved: "workflow" | "subagent" | "skill" |
  // "plugin_command"…) — shown as a badge in the composer palette so the operator knows
  // what kind of thing /name runs (#1660). Client-registered commands carry none.
  kind?: string;
};

export type SettingsField = {
  key: string;
  label: string;
  // "text" = a scalar multiline string (#964), rendered as a textarea but saved like "string".
  type: "string" | "text" | "number" | "bool" | "select" | "string_list" | "secret";
  section: string;
  description?: string;
  restart: boolean;
  options: string[];
  // Where `options` come from: "models" / "models+acp" → the gateway's model list. The Model
  // settings "Get models" action (#1386) merges a freshly-probed list into these fields.
  options_source?: string;
  default?: unknown;
  value?: unknown; // absent for secrets
  is_set?: boolean; // secrets only
  minimum?: number;
  maximum?: number;
  // #963 — conditional visibility: hide this field until a sibling field's current
  // form value satisfies the predicate. `key` is the sibling's full dotted key.
  // {equals}: strict equality · {in}: membership · neither: sibling is truthy.
  depends_on?: { key: string; equals?: unknown; in?: unknown[] };
  // Cascade layer this field's shared default lives at (ADR 0047): "agent" (the
  // per-agent leaf) or "host" (the box-shared host-config.yaml). Where the
  // Settings UI writes a "save to default" edit.
  scope: "agent" | "host";
  // Which cascade layer the live value came from: set in the agent leaf, inherited
  // from the host file, or the App (dataclass) default. Drives the
  // inherited-vs-overridden badge + the "reset to inherited" affordance.
  source: "agent" | "host" | "default";
};

export type SettingsGroup = {
  section: string;
  category?: string;
  fields: SettingsField[];
  test?: { endpoint: string };  // ADR 0029 — generic "Test connection" button
  // The owning plugin id for a plugin-contributed group (ADR 0059) — lets the
  // Plugins surface fold this group into that plugin's Installed row.
  plugin_id?: string;
  guide_url?: string;  // ADR 0059 — optional setup-guide link rendered next to the group
};

// External secrets manager (ADR 0080) — GET /api/secrets/status + POST /api/secrets/sync.
// Names only, never secret values; `vars` are the env vars the hydrator currently owns.
export type SecretsStatus = {
  enabled: boolean;
  provider: string;
  host: string;
  project_id: string;
  environment: string;
  path: string;
  ok: boolean;
  error: string;
  error_kind: string;
  fetched_at: string;
  applied: number;
  shadowed: string[];
  refresh_seconds: number;
  vars: string[];
};

// POST /api/secrets/test — fetch-only connection probe; applies nothing.
export type SecretsTestResult = {
  ok: boolean;
  error: string;
  error_kind: string;
  count: number;
  names: string[];
};

export type WorkflowSummary = {
  name: string;
  description: string;
  inputs: { name: string; required: boolean; default?: unknown }[];
  steps: { id: string; subagent: string; depends_on: string[] }[];
};

export type WorkflowRunResult = {
  output: string;
  steps: Record<string, string>;
  failed: string[];
  // Present on a run that reached a `gate: human` step (F2/F3): the run parked
  // instead of finishing. `run_id` identifies the durable, resumable record.
  run_id?: string;
  paused?: boolean;
  paused_step?: string;
};

// A workflow run parked at a `gate: human` step, awaiting operator approval (F3).
// GET /api/plugins/workflows/runs returns these — the console's "Pending Gates" queue.
// `prompt` is the parked step's RENDERED prompt (inputs + prior outputs substituted),
// so the card shows what will actually run — never raw `{{...}}` template syntax.
export type WorkflowPausedRun = {
  run_id: string;
  recipe_name: string;
  paused_step: string;
  prompt: string;
  step_outputs: Record<string, string>;
  inputs: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type InboxItem = {
  id: number;
  created_at: string;
  priority: "now" | "next" | "later";
  source: string | null;
  text: string;
  dedup_key: string | null;
  delivered_at: string | null;
};

export type ActivityMessage = { role: "user" | "assistant"; content: string };

// One provenance feed entry (ADR 0022): an agent-initiated turn + what triggered it.
export type ActivityEntry = {
  id: number;
  created_at: string;
  origin: string;        // scheduler | inbox | webhook | a2a | operator
  trigger: string;       // job id / inbox source (human label), may be ""
  priority: string;      // inbox tier when applicable, else ""
  state: string;
  text: string;          // the agent's RESPONSE
  task_id: string;
  /** The triggering input text this turn is a response to (scheduled prompt / inbound
   *  message / webhook body), truncated. Shown as "in response to …" (#1375). */
  stimulus?: string;
};

export type ActivityHistory = {
  context_id: string;
  entries: ActivityEntry[];
  messages: ActivityMessage[];
};

export type GoalState = {
  session_id: string;
  condition: string;
  status: string;
  verifier?: { type?: string } & Record<string, unknown>;
  // Completion contract (ADR 0073) — a structured layer OVER the verifier that shapes
  // each drive-turn continuation prompt (the verifier still decides DONE). All optional;
  // absent on contract-less goals. Part 2's goal-creation form sets these; the GoalsPanel
  // can surface them.
  outcome?: string; // the single required end-state (human summary; defaults to condition)
  constraints?: string[]; // invariants that must NOT change/regress
  boundaries?: string[]; // files/dirs/systems in scope
  stop_when?: string; // condition under which the agent pauses and asks the operator
  iteration?: number;
  max_iterations?: number;
  // Per-goal patience (ADR 0030 D4): consecutive no-progress checks vs the limit that
  // finishes the goal `unachievable`. The drawer shows "stalled N/limit" when streak > 0.
  no_progress_streak?: number;
  no_progress_limit?: number | null;
  fresh_context?: boolean; // Ralph loop: each continuation starts a clean thread
  abandon_reason?: string; // set by the agent's abandon_goal tool; drives a terminal "unachievable"
  last_reason?: string;
  last_evidence?: string;
  history?: GoalEvent[]; // per-iteration verifier trail (the drive-loop timeline), oldest→newest
  started_at?: number;
  finished_at?: number | null; // terminal time (epoch seconds); set alongside a terminal status
};

// One entry in a goal's drive-loop timeline (GoalState.history) — the verifier's verdict on
// a single iteration. `status` is "continue" for an ongoing turn or the terminal status
// (achieved / exhausted / unachievable) on the last one.
export type GoalEvent = {
  iteration: number;
  at?: number; // epoch seconds
  status: string;
  reason?: string;
  evidence?: string;
};

// A passive watch (ADR 0067) — a verifier-only objective polled out-of-band. Unlike a goal
// (driven via a bounded continuation loop, one per session), you can hold MANY watches at
// once, keyed by their own `id`. Mirrors the backend `Watch` (graph/watches/types.py).
export type WatchState = {
  id: string;
  condition: string;
  status: string; // active | met | expired | cleared
  verifier?: { type?: string } & Record<string, unknown>;
  interval_s?: number | null; // per-watch cadence override; null → config watch_interval
  deadline?: number | null; // epoch seconds; past → expired
  stall_after?: number | null; // N unchanged checks → on_stalled
  run_prompt?: string; // on met, enqueued as a one-shot turn in run_session
  run_session?: string;
  last_reason?: string;
  last_evidence?: string;
  last_checked?: number | null; // last out-of-band verifier check (epoch seconds)
  finished_at?: number | null; // met/expired time (epoch seconds); the met path sets THIS, not last_checked
  created_at?: number;
};

export type ScheduledJob = {
  id: string;
  prompt: string;
  schedule: string;
  agent_name?: string;
  created_at?: string;
  next_fire?: string | null;
  last_fire?: string | null;
  enabled?: boolean;
  /** IANA tz the cron is evaluated in (recurring jobs only); null/absent = UTC. */
  timezone?: string | null;
};

export type Subagent = {
  name: string;
  description: string;
  enabled: boolean;
  tools: string[];
  default_tools: string[];
  max_turns: number;
  default_max_turns: number;
};

// A live wired tool (Agent → Tools): its source (core/plugin/mcp) + the subsystem
// category it's grouped under in the console. `enabled: false` = present in the
// assembled catalog but dropped by the tools.disabled denylist (still listed so the
// operator can toggle it back on).
export type ToolInfo = {
  name: string;
  description: string;
  source: "core" | "plugin" | "mcp";
  category?: string;
  enabled: boolean;
};

export type ToolCall = {
  id: string;
  name: string;
  input?: string;
  output?: string;
  status: "running" | "done" | "error";
  /** Client wall-clock when the start frame arrived (ms epoch). */
  startedAt?: number;
  /** Elapsed start→end, stamped client-side when the end frame arrives. */
  durationMs?: number;
  /** id of the enclosing `task` tool, if this call ran inside a subagent. */
  parentId?: string;
};

/** Wire shape of a single tool event streamed over the A2A tool-call DataPart. */
export type ToolEvent = {
  id: string;
  name: string;
  phase: "start" | "end";
  input?: string;
  output?: string;
  error?: boolean; // an "end" that failed (phase "failed" on the wire) → card shows the X
  /** id of the enclosing `task` delegation when this is a subagent's own tool call —
   *  set server-side so nesting is explicit (by id), not inferred from frame order. */
  parentId?: string;
};

// A background subagent job (ADR 0050) as returned by GET /api/background and
// carried (partially) on the background.{started,completed} bus events.
export type BackgroundJobDTO = {
  id: string;
  status: "running" | "completed" | "failed" | "canceled";
  subagent_type: string;
  description: string;
  origin_session?: string;
  result?: string;
  created_at?: string;
  completed_at?: string;
};

// A renderable UI component (ADR 0051 Slice 2) carried on a component-v1 DataPart and
// rendered inline by the curated chat component registry.
export type ComponentSpec = { component: string; props: Record<string, unknown> };

// An ordered render block of an assistant turn (bug-fix: preserve text↔tool order).
// A run of answer text, or a group of consecutive top-level tool calls, in emission
// order — so a pre-tool preamble renders ABOVE the tool cards and post-tool text
// below them. `ids` reference the canonical entries in `ChatMessage.toolCalls`, so
// live status + subagent nesting stay in one place (resolved at render).
export type ChatPart =
  | { kind: "text"; text: string }
  | { kind: "reasoning"; text: string }
  | { kind: "tools"; ids: string[] }
  // An inline component (component-v1) at its emission position, so it renders ABOVE the
  // answer text that streams in after it — not shoved below (#1323).
  | { kind: "component"; spec: ComponentSpec };

/** Per-turn token usage + cost, accumulated across the turn's LLM calls — lifted off the
 *  terminal cost-v1 extension (A2A ext, ADR 0006 — URI-keyed artifact metadata). `inputTokens` is the SUM of prompt tokens
 *  across the turn's calls (so a tool-loop turn counts each model call's prompt), NOT the live
 *  context-window fill; it's a per-turn spend/size readout, not a context-fullness gauge. */
export type TurnUsage = {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  /** Prompt tokens served from the model's cache (subset of inputTokens). */
  cacheReadTokens: number;
  /** Prompt tokens written to the cache this turn (subset of inputTokens). */
  cacheCreationTokens: number;
  costUsd?: number;
  durationMs?: number;
};

/** Live context-window readout for a turn (terminal context-v1 DataPart, #1372). Unlike
 *  TurnUsage (per-turn spend), `contextTokens` is the PEAK single-call prompt size — the
 *  actual context-window fill. `compactionAtTokens` is the absolute summarization threshold
 *  when the operator's trigger is token-based (`tokens:N`); fraction:/messages: triggers have
 *  no surfaceable token denominator (the gateway exposes no per-model window), so the meter
 *  shows the size without a bar. */
export type ContextWindow = {
  contextTokens: number;
  compactionAtTokens?: number;
  maxTokens?: number;
  /** The configured compaction trigger string, for the tooltip (e.g. "tokens:120000"). */
  trigger?: string;
  enabled?: boolean;
};

/** Emphasis tone for a local SYSTEM NOTE (a role-"system" message without a `report`) —
 *  e.g. a slash-command confirmation, a status line, or a warning. The reusable seam for
 *  posting non-agent, in-thread notices is `ChatSurface.noteToThread(text, { tone })`,
 *  exposed to forks via the slash + composer registries. Add a tone here + a matching
 *  `.chat-note--<tone>` rule in chat.css; nothing else needs to change. */
export type SystemNoteTone = "info" | "warning" | "danger" | "success";

export type ChatMessage = {
  id?: string;
  role: "user" | "assistant" | "system";
  content: string;
  toolCalls?: ToolCall[];
  components?: ComponentSpec[];
  /** Ordered render blocks (text runs + tool groups) built during streaming so the
   *  text/tool-call order is preserved. Absent on history-loaded messages, which fall
   *  back to the grouped reasoning→toolCalls→content layout. */
  parts?: ChatPart[];
  /** Streamed scratch_pad reasoning ("thinking") — rendered as a collapsible block
   *  above the answer; never part of `content`. */
  reasoning?: string;
  createdAt?: number;
  status?: "streaming" | "done" | "error";
  /** A2A task id for this turn — persisted so a stuck `streaming` message can be
   *  reconciled against the server's task state on reload (self-heal). */
  taskId?: string;
  /** Background-agent report (ADR 0050/0062): the spawning job's id + title. The bubble
   *  shows the server's preview; this lets the card open the FULL report in the document
   *  viewer (fetched by id) instead of forcing a trip to the Activity/Background panel. */
  report?: { jobId: string; title: string };
  /** This turn's token usage + cost (terminal cost-v1 extension metadata). Shown as a small footer
   *  under the answer; absent on user turns and history saved before this shipped. */
  usage?: TurnUsage;
  /** This turn's context-window fill + compaction threshold (terminal context-v1 DataPart).
   *  Drives the meter in the same footer; absent on user turns / pre-ship history. */
  contextWindow?: ContextWindow;
  /** Set on a local SYSTEM NOTE (role "system", no `report`) to tone its rendering — a
   *  reusable, non-agent in-thread notice (slash-command confirmation, status, warning).
   *  Posted via `noteToThread(text, { tone })`; system notes never carry the answer
   *  action row (copy/fork/regenerate) — those are answer-only. */
  noteTone?: SystemNoteTone;
};

// HITL (human-in-the-loop) request surfaced when a turn pauses as input-required
// — a `request_user_input` JSON-schema form (kind "form", multi-step = wizard) or
// an `ask_human` free-text question.
export type HitlFormStep = {
  schema: Record<string, unknown>; // JSON Schema (draft-07) of the step's fields
  uiSchema?: Record<string, unknown>;
  title?: string;
  description?: string;
};
export type HitlPayload = {
  kind?: "form" | "approval";
  title?: string;
  description?: string;
  steps?: HitlFormStep[];
  question?: string; // ask_human shape
  detail?: string; // approval shape — the command/action being approved
  // #1701 Slice 2: set when this input-required is a PLUGIN composer-form (not a graph
  // interrupt). The console redeems the answers via POST /api/chat/commands/submit with
  // this id instead of resuming the agent graph.
  plugin_callback_id?: string;
};


export type Task = {
  id: string;
  title: string;
  status?: string;
  description?: string;
  priority?: number | string;
  issue_type?: string;
  type?: string;
  assignee?: string;
  created_at?: string;
  updated_at?: string;
  closed_at?: string | null;
};

export type AgentConfig = {
  // Where turns run: "native" (the built-in LangGraph loop on the model gateway) or
  // "acp:<agent>" (hand each turn to a CLI coding agent over ACP — ADR 0033).
  agent_runtime?: string;
  model: {
    provider: string;
    name: string;
    api_base: string;
    api_key?: string;
    temperature: number;
    max_tokens: number;
    max_iterations: number;
  };
  subagents: {
    researcher: {
      enabled: boolean;
      tools: string[];
      max_turns: number;
    };
  };
  middleware: {
    knowledge: boolean;
    audit: boolean;
    memory: boolean;
    scheduler: boolean;
  };
  knowledge: {
    db_path: string;
    embed_model: string;
    top_k: number;
  };
  identity: {
    name: string;
    operator: string;
  };
  auth: {
    token: string;
  };
  discord?: {
    enabled: boolean;
    bot_token?: string;
    admin_ids: string[];
  };
  runtime: {
    autostart_on_boot: boolean;
  };
  operator?: {
    allowed_dirs: string[];
    project_dir?: string;
  };
  plugins?: {
    enabled?: string[];
  };
};

export type ConfigPayload = {
  config: AgentConfig;
  soul: string;
};

export type SetupStatus = {
  setup_complete: boolean;
  presets: string[];
};

// Telemetry (ADR 0006 Slice 3) — mirrors /api/telemetry/* (telemetry_store.py).
export type TelemetrySummary = {
  turns: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  llm_calls: number;
  tool_calls: number;
  avg_duration_ms: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
  success_rate: number;
  cache_hit_ratio: number;
  by_model: { model: string; turns: number; cost_usd: number; total_tokens: number }[];
};

export type TelemetryTurn = {
  task_id: string;
  session_id: string;
  state: string;
  success: number;
  model: string;
  models?: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  duration_ms: number;
  llm_calls: number;
  tool_calls: number;
  created_at: string;
  ended_at: string;
  // Langfuse trace for this turn — empty/absent when tracing was off. Paired
  // with /api/telemetry/recent's `langfuse_trace_url_template` to deep-link.
  trace_id?: string | null;
};

export type TelemetryInsights = {
  turns: number;
  flagged: (TelemetryTurn & { reasons: string[] })[];
  flagged_count: number;
  levers: {
    cache: { hit_ratio: number; read_tokens: number; est_savings_usd: number };
    routing: { by_model: { model: string; turns: number; cost_usd: number; total_tokens: number }[] };
    success_rate: number;
  };
  unproven_levers: string[];
};

// Playbooks (skills surface, ADR 0009) — mirrors /api/playbooks (skills.db).
export type Playbook = {
  id: number;
  name: string;
  description: string;
  tools_used: string[];
  source: string;        // "disk" (pinned SKILL.md) | "emitted" (agent-learned)
  confidence: number;
  last_used: string | null;
  created_at: string | null;
  // Tier (ADR 0041 layered skills): "private" | "commons". Present only when the
  // index is layered (the agent reads a shared commons ∪ its private library);
  // absent in scoped/shared mode, where there's a single library and no promote.
  tier?: "private" | "commons";
  // Skills CRUD — the list route tags each row so the UI shows edit/delete only on
  // skills the operator owns: "user" (authored SKILL.md) and "learned" (agent-
  // emitted) are editable; "bundled" (shipped example) and "commons" are read-only.
  origin?: "user" | "learned" | "bundled" | "commons";
  editable?: boolean;
  user_facing?: boolean;
  // User-only (2026-06): a `/<slash>` command withheld from the agent's retrieval.
  user_only?: boolean;
  slash?: string;
  // Full procedure body — present only on the single-skill detail (GET
  // /api/playbooks/:id); the list payload omits it to stay light.
  prompt_template?: string;
};

// One row from the knowledge store (knowledge/store.py chunks table), as the
// searchable Knowledge → Store view consumes it (ADR 0020).
export type KnowledgeChunk = {
  id: number;
  heading: string;
  content: string;
  preview: string;
  domain: string;
  source: string | null;
  source_type: string | null;
  finding_type: string | null;
  created_at: string | null;
  // BM25/RRF relevance — the backend always sends it (null on plain listings).
  score?: number | null;
  // Tier (ADR 0041 / bd-2wu): "private" | "commons" — present only when the store is
  // layered (commons ∪ private); null otherwise. Drives the tier badge + promote/unshare.
  tier?: "private" | "commons" | null;
};

// One hot-memory row (GET /api/memory/hot): a knowledge chunk plus whether it's
// inside the CURRENT per-turn injection window (the newest ~100 domain="hot"
// chunks under the character budget). Absent on backends that predate the field
// (or custom stores without get_hot_memory_entries) → unknown; the console only
// draws the "not injecting" badge on an explicit `false`.
export type MemoryHotChunk = KnowledgeChunk & { injecting?: boolean };

// Memory inspector (ADR 0069 D7) — the delivery-layer audit surface.
// One session-summary digest row (GET /api/memory/sessions) — the same
// derivation the <prior_sessions> digest injects, so the list can't drift
// from what the agent is actually told.
export type MemorySessionDigest = {
  session_id: string;
  timestamp: string;
  surface: string; // chat | background | a2a | …
  topic: string;
  message_count: number;
  size_bytes?: number;
  // Whether this session is in the CURRENT <prior_sessions> digest window (the
  // ~10 newest under the token cap, background:* excluded). Absent on older
  // backends → unknown; only an explicit `false` draws the "not in digest" badge.
  in_digest?: boolean;
  // Detail-only fields (GET /api/memory/sessions/{id}):
  trace_id?: string | null;
  rendered?: string; // the full render recall_session returns
};

// One per-model-call injection record (GET /api/memory/injections, ADR 0069 D6):
// which memory items entered which turn — the poisoning-forensics trail.
export type MemoryInjectionRow = {
  id: number;
  ts: string;
  session_id: string;
  digest_session_ids: string[];
  hot_chunk_ids: number[];
  rag_chunk_ids: number[];
  approx_tokens: number;
};

// The RESOLVED detail for one injection record (GET /api/memory/injections/{id}):
// the id arrays turned into their referenced content, grouped for the detail
// dialog. An item whose underlying chunk was pruned/deleted comes back
// `unavailable` (the dialog shows "no longer stored") rather than dropping it.
export type InjectionPastSession = { id: string; title: string | null };
export type InjectionMemoryItem = {
  id: number;
  heading: string | null;
  snippet: string | null;
  unavailable: boolean;
};
export type InjectionDocItem = {
  id: number;
  source: string | null;
  snippet: string | null;
  unavailable: boolean;
};
export type MemoryInjectionDetail = {
  ts: string;
  session_id: string;
  past_sessions: InjectionPastSession[];
  memories: InjectionMemoryItem[];
  docs: InjectionDocItem[];
  approx_tokens: number;
};

// Delegate registry (ADR 0025) — the agents & endpoints the agent can talk to.
export type DelegateFieldSpec = {
  key: string;
  label: string;
  kind: string; // text | secret | args | path | number | textarea | select | envmap
  required: boolean;
  help: string;
  placeholder: string;
  options: string[];
  default?: unknown;
};
export type DelegateTypeSpec = { type: string; label: string; blurb: string; fields: DelegateFieldSpec[] };
// A known ACP coding agent from the canonical backend catalog (/api/acp-agents) — the
// single source for the Delegates picker + the setup wizard's runtime choices.
export type AcpAgent = { id: string; label: string; command: string; args: string[] };
export type DelegateProbe = { ok: boolean | null; latency_ms?: number; error?: string; detail?: string; checked_at?: number };
export type DelegateView = {
  name: string;
  type: string;
  description: string;
  configured: boolean;
  error: string | null;
  has_secret: boolean;
  // True when any per-row env secret is stored (#2114) — the form shows those rows
  // set-but-masked. The masked env values come back as "***" in the `env` map.
  has_env_secrets?: boolean;
  health?: DelegateProbe;
  [key: string]: unknown;
};

// Fleet (ADR 0042) — many workspace agents on one host, switchable in place.
export type FleetAgent = {
  name: string; // also the instance id; unique, [A-Za-z0-9-_]
  id: string;
  port: number;
  pid: number | null; // null when stopped
  running: boolean;
  bundle: string; // "" for a Basic agent
  a2a?: string; // the agent's own A2A endpoint (focus-independent)
  host?: boolean; // the instance serving this console — can't be stopped/removed from itself
  // Only ever set on the `host` entry, by the instance itself: this instance is a workspace
  // member SPAWNED by another hub's supervisor (its instance root carries a workspace.yaml).
  // Consoles reaching a member directly use it to gate hub-only affordances (#1708).
  member?: boolean;
  remote?: boolean; // a REMOTE member (ADR 0042 §I) — proxied by URL, no start/stop from here
  url?: string; // the remote member's base URL
  // App version (pyproject [project].version). Always set on the host (hub) entry;
  // for a remote member it's the last-probed value ("" until the first probe lands).
  // The console flags hub↔remote skew — the proxied /api/* surface has no other versioning.
  version?: string;
};

// The focused agent is the URL slug now (ADR 0042 slug routing) — no server-side 'active'.
export type FleetStatus = { agents: FleetAgent[] };

// Another protoAgent found on the box / LAN (ADR 0042 §I) — a candidate remote delegate.
export type DiscoveredAgent = { name: string; url: string; host: string; port: number };

export type Archetype = {
  id: string; // "basic"/"custom", or a bundle id e.g. "product-stack"
  label: string;
  icon: string; // lucide-react icon name
  blurb: string;
  bundle: string | null; // null = Basic; else the bundle git URL
  soul: string; // base SOUL.md the wizard seeds when this archetype is picked ("" = none)
  // Host capabilities the archetype needs to be USEFUL (#2186 follow-on) — e.g.
  // "python_runtime" (cowork's document skills route through execute_code, which on
  // the desktop app needs the managed CPython). The picker warns at choose-time when
  // a requirement isn't provisioned. Optional: absent on older hosts.
  requires?: string[];
};

// What an archetype's bundle would set up — the read-only pre-install peek
// served by GET /api/archetypes/{id}/preview. `bundle: null` = code-free persona.
export type ArchetypePreviewMember = {
  id: string | null;
  builtin: boolean;
  ref?: string | null;
  url?: string | null;
  name?: string;
  version?: string;
  description?: string;
  requires_pip?: string[];
  capabilities?: Record<string, unknown>;
  views?: string[];
  skills?: { name: string; description: string }[];
  error?: string; // member unreachable — the rest of the preview still renders
};
export type ArchetypePreview = {
  id: string;
  bundle: {
    kind: "bundle" | "plugin";
    id?: string;
    name?: string;
    description?: string;
    verified_against?: string;
    enabled?: string[];
    members: ArchetypePreviewMember[];
    // What the bundle will ask the operator to fill (#2041): catalog-shaped MCP servers
    // (each with `${input}` placeholders + their `inputs` spec) and the standalone secrets
    // it declares. Surfaced up front so the preview can show them and the new-agent
    // Configure step can collect them WITHOUT installing (read-only peek).
    mcp?: McpCatalogEntry[];
    secrets?: McpCatalogInput[];
  } | null;
};

// Developer flags (ADR 0068) — the /api/flags payload the Developer panel renders.
export type FlagTier = "off" | "dev" | "beta" | "on";
export type FlagChannel = "prod" | "beta" | "dev";
export type FlagInfo = {
  id: string;
  description: string;
  tier: FlagTier;
  owner: string;
  remove_by: string;
  enabled: boolean; // channel-resolved (before any device-local override)
  source: "channel" | "env";
};
export type FlagsPayload = { channel: FlagChannel; flags: FlagInfo[] };

// A recorded SOUL.md persona snapshot (#1691). `saved_at` is ISO-8601 UTC (or "" if
// unparseable); `is_current` marks the one whose text matches the live persona.
export type SoulVersion = {
  id: string;
  saved_at: string;
  size: number;
  preview: string;
  is_current: boolean;
};

// Managed Node runtime (ADR 0085) — the /api/runtime/node payload. `source` is which
// Node would actually launch npx today (a user's own install wins over a managed one);
// `install` tracks any in-flight provisioning the card polls.
export type NodeRuntimeStatus = {
  source: "system" | "managed" | null;
  version: string | null;
  bin_dir: string | null;
  managed: boolean;
  managed_version: string | null;
  system: boolean;
  supported: boolean;
  target_version: string;
};

export type NodeInstallState = {
  state: "idle" | "running" | "done" | "error";
  pct: number;
  message: string;
  error: string | null;
};

export type NodeRuntimePayload = { node: NodeRuntimeStatus; install: NodeInstallState };

// Managed Python runtime (ADR 0094) — the execute_code child interpreter on the packaged
// desktop app. `needed` is whether THIS backend would use it (frozen builds only — source
// runs spawn their own interpreter, so the card stays hidden there); the baseline flags
// track the document libraries pip-installed into the runtime's own site-packages.
// The install-progress shape is shared with the Node runtime (same endpoint contract).
export type PythonRuntimeStatus = {
  needed: boolean;
  managed: boolean;
  managed_version: string | null;
  exe: string | null;
  baseline_installed: boolean;
  baseline_current: boolean;
  supported: boolean;
  target_version: string;
};

export type PythonRuntimePayload = { python: PythonRuntimeStatus; install: NodeInstallState };
