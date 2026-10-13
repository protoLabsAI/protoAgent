/**
 * Setup + settings: config/SOUL, providers + OAuth, agent snapshots (ADR 0091), connection
 * tests, secrets (ADR 0080), the settings cascade (ADR 0047), flags and theme.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  SnapshotImportPlan,
  SnapshotImportResult,
  SnapshotReview,
  AgentConfig,
  ConfigPayload,
  FlagsPayload,
  SecretsStatus,
  SecretsTestResult,
  SetupStatus,
  SettingsGroup,
  SoulVersion,
} from "../types";
import { apiUrl, applyAuth } from "./routing";
import { request, requestForm } from "./http";

export const setupApi = {
  setupStatus() {
    return request<SetupStatus>("/api/config/setup-status");
  },

  config() {
    return request<ConfigPayload>("/api/config");
  },

  soulPreset(name: string) {
    return request<{ name: string; content: string }>(`/api/config/presets/${encodeURIComponent(name)}`);
  },

  // SOUL.md version history (#1691): every persona save archives the outgoing text.
  soulHistory() {
    return request<{ versions: SoulVersion[] }>("/api/config/soul/history");
  },
  soulVersion(id: string) {
    return request<{ id: string; content: string }>(`/api/config/soul/history/${encodeURIComponent(id)}`);
  },
  restoreSoulVersion(id: string) {
    return request<{ ok: boolean; messages: string[]; restored: string }>(
      `/api/config/soul/history/${encodeURIComponent(id)}/restore`,
      { method: "POST" },
    );
  },

  models(apiBase: string, apiKey: string, provider = "") {
    return request<{ models: string[]; error: string }>("/api/config/models", {
      method: "POST",
      // `provider` (ADR 0097): a native OAuth provider lists the subscription
      // account's models instead of the gateway's; blank = gateway.
      body: { api_base: apiBase, api_key: apiKey, provider },
    });
  },

  /** The provider registry (ADR 0106) — every configured CONNECTION, keys redacted.
   *  `in_use_by` names the slots routing through each one, so the delete guard is
   *  legible before the operator tries it. */
  providers() {
    return request<{
      providers: {
        id: string;
        type: string;
        label?: string;
        base_url?: string;
        display: string;
        has_key: boolean;
        in_use_by: string[];
        // The same dependencies `in_use_by` names, structured so the panel can offer a
        // repoint/clear per row (bd-v6xy). `kind`: slot | favorite | subagent. `clearable`
        // is false for `model.name` only (the lead model must always resolve). A favorites
        // entry is ONE row whose `value` is the matching favorite list. Optional so a
        // pre-bd-neiz backend (or a test fixture) that omits it still types.
        in_use?: {
          key: string;
          value: string | string[];
          kind: "slot" | "favorite" | "subagent";
          clearable: boolean;
        }[];
      }[];
    }>("/api/config/providers");
  },

  addProvider(body: { id: string; type: string; label?: string; base_url?: string; api_key?: string }) {
    return request<{ ok: boolean; id: string }>("/api/config/providers", { method: "POST", body });
  },

  /** Label / endpoint / key only. There is no id or type here on purpose: both are
   *  identity, and an id lives inside stored model values that a rename cannot reach. */
  updateProvider(id: string, body: { label?: string; base_url?: string; api_key?: string }) {
    return request<{ ok: boolean; id: string }>(`/api/config/providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
    });
  },

  /** 409 with the referencing slots named when the connection is still in use.
   *
   *  `releases` (bd-v6xy) repoints or clears each blocking reference in the SAME request
   *  that removes the connection — `<other_pid>:<model>` to repoint, `null` to clear
   *  (favorites: drop those prefixed `<pid>:`). It is sent as the JSON body ONLY when
   *  provided; the bare `removeProvider(id, confirmLast)` call stays byte-identical (no
   *  body), preserving the old refuse-if-in-use behaviour. */
  removeProvider(id: string, confirmLast = false, releases?: Record<string, string | null>) {
    const query = confirmLast ? "?confirm_last=true" : "";
    return request<{ ok: boolean; removed: string; released?: string[] }>(
      `/api/config/providers/${encodeURIComponent(id)}${query}`,
      releases ? { method: "DELETE", body: { releases } } : { method: "DELETE" },
    );
  },

  /** That connection's own model list — its endpoint, or its subscription account. */
  providerModels(id: string) {
    return request<{ models: string[]; error: string }>(
      `/api/config/providers/${encodeURIComponent(id)}/models`,
      { method: "POST" },
    );
  },

  /** Sign-in status for the native OAuth providers (ADR 0097) — "✓ signed in" or a
   *  sign-in hint per provider, so the setup UX never asks for a key it doesn't need. */
  oauthStatus() {
    return request<{
      providers: { provider: string; signed_in: boolean; source: string; detail: string; hint: string }[];
    }>("/api/config/oauth-status");
  },

  /** Begin an in-console OAuth sign-in (ADR 0097). `mode: "device"` (Codex) returns a
   *  user_code + verification_uri to poll; `mode: "redirect"` (Claude) returns an
   *  authorize_url to open and complete with the pasted code. */
  oauthStart(provider: string) {
    return request<{
      flow_id: string;
      mode: "device" | "redirect";
      user_code?: string;
      verification_uri?: string;
      interval?: number;
      authorize_url?: string;
    }>("/api/config/oauth/start", { method: "POST", body: { provider } });
  },
  /** Poll a Codex device sign-in until the user approves. `graph_reloaded` (#2458):
   *  a completed sign-in on a graphless server rebuilt the graph inline. */
  oauthPoll(flowId: string) {
    return request<{ status: "pending" | "complete" | "error"; error?: string; graph_reloaded?: boolean; graph_reload_error?: string }>(
      "/api/config/oauth/poll",
      { method: "POST", body: { flow_id: flowId } },
    );
  },
  /** Complete a Claude sign-in with the pasted `code#state`. */
  oauthComplete(flowId: string, code: string) {
    return request<{ status: "complete" | "error"; error?: string; graph_reloaded?: boolean; graph_reload_error?: string }>(
      "/api/config/oauth/complete",
      { method: "POST", body: { flow_id: flowId, code } },
    );
  },
  /** Abandon an in-progress sign-in server-side (#2440) — so wizard Cancel truly cancels
   *  the flow, not just the browser timer. */
  oauthCancel(flowId: string) {
    return request<{ ok: boolean; cancelled: boolean }>(
      "/api/config/oauth/cancel",
      { method: "POST", body: { flow_id: flowId } },
    );
  },
  /** Disconnect a native OAuth provider (#2440): best-effort remote revoke + delete
   *  protoAgent's own credential + suppress auto-reconnect until the next sign-in. */
  oauthDisconnect(provider: string) {
    return request<{ provider: string; removed: boolean; revoked: boolean; note: string; graph_unloaded?: boolean }>(
      "/api/config/oauth/disconnect",
      { method: "POST", body: { provider } },
    );
  },

  // ── Agent snapshot (ADR 0091 Slice 1) ──
  /** Review WITHOUT building the download: what would be stripped, what the target must
   *  re-supply, what the pattern sweep matched. The export is meant to leave the machine,
   *  so the console shows this first rather than handing over a zip nobody has read. */
  snapshotReview() {
    return request<SnapshotReview>("/api/agent/export", { method: "POST", body: { dry_run: true } });
  },
  /** The snapshot itself. Returns the Blob plus the server's filename — the name carries the
   *  agent + timestamp, and re-deriving it client-side would drift from the artifact. */
  async exportSnapshot(): Promise<{ blob: Blob; filename: string; definitionSha256: string }> {
    const res = await fetch(apiUrl("/api/agent/export"), {
      method: "POST",
      headers: applyAuth(new Headers({ "content-type": "application/json" })),
      body: JSON.stringify({ dry_run: false }),
    });
    if (!res.ok) throw new Error(`export failed: ${res.status}`);
    const disposition = res.headers.get("content-disposition") || "";
    const match = /filename="([^"]+)"/.exec(disposition);
    return {
      blob: await res.blob(),
      filename: match?.[1] || "agent-snapshot.zip",
      definitionSha256: res.headers.get("x-snapshot-definition-sha256") || "",
    };
  },

  /** Inspect a snapshot WITHOUT applying it (ADR 0091 D3). Returns the plan: which plugins
   *  would be installed and run, which capabilities the config grants, which credentials the
   *  new agent needs. Writes nothing — the console shows this before asking for consent. */
  snapshotPlan(file: File) {
    const form = new FormData();
    form.append("file", file);
    return requestForm<SnapshotImportPlan>("/api/agent/import", form);
  },
  /** Apply a snapshot. `acknowledged` asserts the operator has SEEN the plan — applying
   *  installs and runs the plugin code it names, so this is never sent implicitly. */
  snapshotImport(file: File, opts: { name: string; secrets: Record<string, string> }) {
    const form = new FormData();
    form.append("file", file);
    form.append("name", opts.name);
    form.append("acknowledged", "true");
    if (Object.keys(opts.secrets).length) form.append("secrets_json", JSON.stringify(opts.secrets));
    return requestForm<SnapshotImportResult>("/api/agent/import", form);
  },

  // lists). Blank fields fall back to the saved config (Settings re-test).
  testModel(apiBase: string, apiKey: string, model: string, provider = "") {
    return request<{ ok: boolean; error: string }>("/api/config/test-model", {
      method: "POST",
      // `provider` (ADR 0097): a native OAuth provider tests through the subscription
      // (a real streamed turn), ignoring api_base/api_key; blank = gateway.
      body: { api_base: apiBase, api_key: apiKey, model, provider },
    });
  },

  // Generic plugin "Test connection" (ADR 0029) — POST the group's fields (short
  // keys) to the plugin's test route. Blank/omitted fields fall back to the saved
  // config. Returns {ok, identity, error}. Used by any group with a `test` endpoint.
  testConfig(endpoint: string, fields: Record<string, unknown>) {
    return request<{ ok: boolean; identity: string | null; error: string | null }>(endpoint, {
      method: "POST",
      body: fields,
    });
  },

  // External secrets manager (ADR 0080) — status / force-a-refresh / connection test.
  // Test runs against the SAVED config (unsaved form edits don't ride along yet).
  secretsStatus() {
    return request<SecretsStatus>("/api/secrets/status");
  },
  secretsSync() {
    return request<SecretsStatus>("/api/secrets/sync", { method: "POST", body: {} });
  },
  secretsTest() {
    return request<SecretsTestResult>("/api/secrets/test", { method: "POST", body: {} });
  },

  // `requires_tools` is the picked archetype's capability contract (ADR 0100) — the
  // host-side twin of what createAgent records on a member's workspace.yaml, so a
  // wizard-installed archetype gets the same contract banner. Always sent (an empty
  // list clears a stale record from an earlier wizard run).
  finishSetup(config: Partial<AgentConfig>, soul: string, requiresTools: string[] = []) {
    return request<{ ok: boolean; message: string }>("/api/config/setup", {
      method: "POST",
      body: { config, soul, requires_tools: requiresTools },
    });
  },

  // Merge-apply a config patch (+ optional SOUL.md) on the live agent, then reload.
  // Partial config is merged into the live YAML (not a replace), so passing just
  // `{ identity: { name } }` is safe. Pass null to skip either.
  applyConfig(config: Partial<AgentConfig> | null, soul: string | null) {
    return request<{ ok: boolean; messages: string[] }>("/api/config", {
      method: "POST",
      body: { config, soul },
    });
  },

  settingsSchema(host = false) {
    return request<{ groups: SettingsGroup[] }>("/api/settings/schema", { host });
  },

  // Save a flat {key: value} payload to a cascade layer (ADR 0047): "agent" (the
  // per-agent leaf, default) or "host" (the box-shared host-config.yaml). Secrets
  // are refused on the host layer server-side.
  saveSettings(
    updates: Record<string, unknown>,
    layer: "agent" | "host" = "agent",
    host = false,
  ) {
    return request<{ ok: boolean; messages: string[]; restart_required: string[] }>("/api/settings", {
      method: "POST",
      body: { updates, layer },
      host,
    });
  },

  // Reset-to-inherited (ADR 0047): pop the given keys from the agent leaf so each
  // falls back to the Host/App layer.
  resetSettings(keys: string[]) {
    return request<{ ok: boolean; messages: string[] }>("/api/settings/reset", {
      method: "POST",
      body: { keys },
    });
  },
  flags() {
    return request<FlagsPayload>("/api/flags");
  },

  // Per-agent theme (ADR 0042). The blob is opaque — the DS ThemePanel owns its schema; the
  // server just round-trips JSON. These auto-route to the focused agent via the active prefix
  // (host → /api/theme, peer → /active/api/theme).
  getTheme() {
    return request<{ theme: unknown | null }>("/api/theme");
  },
  saveTheme(theme: unknown) {
    return request<{ ok: boolean }>("/api/theme", { method: "PUT", body: { theme } });
  },
  resetTheme() {
    return request<{ ok: boolean }>("/api/theme", { method: "DELETE" });
  },
};
