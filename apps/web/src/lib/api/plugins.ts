/**
 * Extensions: delegates (ADR 0025), ACP agents, git-installed plugins + bundles (ADR 0027),
 * and managed MCP servers.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  AcpAgent,
  DelegateProbe,
  DelegateTypeSpec,
  DelegateView,
  CatalogPlugin,
  McpCatalogEntry,
  InstalledPlugin,
  PluginDepsNeeded,
  PluginInstallSummary,
  PluginUpdate,
} from "../types";
import { request } from "./http";

export const pluginsApi = {
  // Delegate registry (ADR 0025) — the agents & endpoints the agent can talk to.
  delegateTypes() {
    return request<{ types: DelegateTypeSpec[] }>("/api/delegate-types");
  },
  // The canonical ACP coding-agent catalog (single source — runtime/acp_agents.py).
  acpAgents() {
    return request<{ agents: AcpAgent[] }>("/api/acp-agents");
  },
  delegates() {
    return request<{ delegates: DelegateView[]; can_share?: boolean }>("/api/delegates");
  },
  // Git-installed plugins (ADR 0027). install fetches code only (does NOT enable).
  installedPlugins() {
    // `bundles` = the lock's bundles[] registry verbatim (#2718) — the authoritative
    // installed-bundle list (a bundle whose members were all removed individually
    // still has a row and is still uninstallable). Optional: absent on older backends.
    // `deps_installing`: the dependency install this server is running (one per environment
    // at a time), so every tab can show it busy. Null when idle; absent on older backends.
    return request<{
      plugins: InstalledPlugin[];
      bundles?: { id: string; name?: string }[];
      deps_installing?: { id: string; target?: string; since?: number } | null;
    }>("/api/plugins/installed");
  },
  // The curated official-plugin directory (Discover, ADR 0059), merged with install
  // state. One-click install posts each entry's `repo` to installPlugin().
  pluginCatalog() {
    return request<{ plugins: CatalogPlugin[] }>("/api/plugins/catalog");
  },
  // Install AUTO-ENABLES + runs the plugin (trust-by-default): `enabled` lists the
  // ids now in plugins.enabled; `reloaded` whether the hot-reload landed; `enable_error`
  // is set if the install succeeded but the enable-reload failed (enable it manually
  // then). `load_errors` (#2716) maps enabled ids that FAILED to import on that reload —
  // in plugins.enabled but not running — optional so older backends parse fine.
  installPlugin(
    url: string,
    ref?: string,
    force?: boolean,
    // Bundle create-time seed values (#2041/#2118/#2934): `inputs` fill the bundle's MCP
    // `${input}` placeholders, `secrets` its declared secrets, `config_inputs` its
    // declared config prompts (written at their dotted config paths) — same body shapes
    // as POST /api/fleet. Omitted → env-only / declared-default seeding.
    seed?: {
      inputs?: Record<string, string>;
      secrets?: { key: string; value: string }[];
      config_inputs?: Record<string, string | boolean>;
    },
  ) {
    return request<{
      installed: PluginInstallSummary;
      enabled: string[];
      reloaded: boolean;
      restart_recommended: boolean;
      enable_error: string | null;
      load_errors?: Record<string, string>;
      // Packages the just-installed plugins still need HERE (install never pips — ADR 0027
      // D4). The console asks once and, on confirm, calls installPluginDeps. Optional so
      // older backends parse fine.
      deps_needed?: PluginDepsNeeded[];
      // Consent gate (ADR 0071 D3, #2721): set INSTEAD of the fields above when the
      // source needs a one-time "this runs code" confirm — nothing was fetched.
      // Ack via ackPluginSource, then retry the install.
      needs_ack?: boolean;
      source?: string;
    }>(
      "/api/plugins/install",
      {
        method: "POST",
        body: {
          url,
          ref: ref || undefined,
          force: force || undefined,
          inputs: seed?.inputs,
          secrets: seed?.secrets,
          config_inputs: seed?.config_inputs,
        },
      },
    );
  },
  // One-time source consent (ADR 0071 D3, #2721): persists the exact normalized repo
  // into plugins.sources.acked (trustAll also flips plugins.trust_unverified — the
  // dialog's "don't ask again"). The caller retries its install afterwards.
  ackPluginSource(url: string, trustAll?: boolean) {
    return request<{ ok: boolean; acked: string | null; trust_all: boolean }>("/api/plugins/ack", {
      method: "POST",
      body: { url, trust_all: trustAll || undefined },
    });
  },
  uninstallPlugin(id: string) {
    // `superseded_by_bundled` (the bundled version) = only the ignored old copy of a
    // plugin that now ships with protoAgent was removed; the built-in keeps running.
    return request<{ ok: boolean; superseded_by_bundled?: string; restart_recommended?: boolean }>(
      `/api/plugins/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },
  // Pip-install a plugin's declared requires_pip (the code-exec step `install`
  // deliberately skips) — previously CLI-only.
  installPluginDeps(id: string) {
    // needs_ack (#2743): deps-install re-checks source trust like install does — the
    // caller renders the same confirm dialog and retries after POST /api/plugins/ack.
    // `failed` (#3450): optional deps that didn't install — they fail soft, so an empty
    // `installed` alone can't distinguish "nothing to do" from "everything failed".
    return request<{ ok?: boolean; installed?: string[]; failed?: string[]; refresh?: "none" | "plugin" | "full"; needs_ack?: boolean; source?: string }>("/api/plugins/install-deps", {
      method: "POST",
      body: { id },
    });
  },
  // Run a setup STEP a plugin registered for its setup-gap banner — a `plugin_setup` action's
  // button ("Download the CLI", "Install Chrome"). The host runs only the callable it holds for
  // exactly this (plugin, step); `pending` means it started long work the gap reports on. A core
  // route OUTSIDE /api/plugins/<id>/, which a plugin's manifest may exempt from the auth gate.
  runPluginSetupStep(plugin: string, step: string) {
    return request<{ ok: boolean; message?: string; pending?: boolean }>(
      `/api/plugin-setup/${encodeURIComponent(plugin)}/${encodeURIComponent(step)}`,
      { method: "POST" },
    );
  },
  // Per-plugin freshness (ADR 0027). The backend TTL-caches the ls-remote probe,
  // so polling is cheap; each row carries behind/pinned/error. `bundles` (#2718,
  // ADR 0049 D4) is the same status per installed bundle — behind there means the
  // bundle REPO's manifest moved (member pins may move with it on update). Optional
  // so older backends parse fine.
  pluginUpdates() {
    return request<{ plugins: PluginUpdate[]; bundles?: PluginUpdate[] }>("/api/plugins/updates");
  },
  // Bundle-level update (#2718): re-resolves the bundle's ref (release-tag pins move
  // to the newest semver), re-pins every member, retires members the new manifest
  // dropped, hot-reloads. The declared enable set re-applies WITHOUT undoing an
  // operator's explicit disable.
  updateBundle(id: string) {
    return request<{
      installed: PluginInstallSummary;
      enabled: string[];
      reloaded: boolean;
      restart_recommended: boolean;
      enable_error: string | null;
      load_errors: Record<string, string>;
      removed_members: string[];
      retire_error: string | null;
    }>(`/api/plugins/bundles/${encodeURIComponent(id)}/update`, { method: "POST" });
  },
  // One-action bundle removal (#2718): exclusively-owned members + the lock row;
  // members shared with another bundle (or re-installed directly) are kept.
  uninstallBundle(id: string, purge?: boolean) {
    return request<{
      ok: boolean;
      removed_members: string[];
      kept: string[];
      reloaded: boolean;
      reload_error: string | null;
    }>(`/api/plugins/bundles/${encodeURIComponent(id)}${purge ? "?purge=true" : ""}`, { method: "DELETE" });
  },
  // Re-clone every locked plugin that's missing on disk (fresh clone / restored
  // data dir). Fetches at the lock's resolved_sha; already-enabled plugins come
  // up live via the same hot-reload the enable toggle uses. "superseded" = the locked
  // copy's source is retired by a bundled plugin of the same id — nothing to fetch.
  syncPlugins() {
    return request<{
      plugins: { id: string; status: "present" | "installed" | "failed" | "superseded"; error?: string }[];
      reloaded: boolean;
      reload_error: string | null;
    }>("/api/plugins/sync", { method: "POST" });
  },
  // Pull the latest code at the plugin's recorded ref + hot-reload (same path as
  // enable). Returns whether the live reload landed and if a restart is still
  // recommended (a view/route plugin can't swap its mounted router in place).
  updatePlugin(id: string) {
    return request<{ ok: boolean; id: string; version?: string; resolved_sha?: string; reloaded: boolean; restart_recommended: boolean }>(
      `/api/plugins/${encodeURIComponent(id)}/update`,
      { method: "POST" },
    );
  },
  setPluginEnabled(id: string, enabled: boolean) {
    return request<{ deps_missing?: string[]; ok: boolean; enabled: boolean; reloaded: boolean; restart_recommended: boolean }>(
      `/api/plugins/${encodeURIComponent(id)}/enabled`,
      { method: "POST", body: { enabled } },
    );
  },
  addMcpServer(entry: Record<string, unknown>) {
    return request<{ ok: boolean; name: string; servers: string[] }>(
      "/api/mcp/servers",
      { method: "POST", body: entry },
    );
  },
  removeMcpServer(name: string) {
    return request<{ ok: boolean; servers: string[] }>(
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
  },
  importMcpServers(raw: string) {
    return request<{ ok: boolean; added: string[]; servers: string[] }>(
      "/api/mcp/servers/import",
      { method: "POST", body: { raw } },
    );
  },
  mcpCatalog() {
    return request<{ servers: McpCatalogEntry[] }>("/api/mcp/catalog");
  },
  promoteMcpServer(name: string) {
    return request<{ ok: boolean; promoted: boolean; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}/promote`,
      { method: "POST" },
    );
  },
  forgetMcpServer(name: string) {
    return request<{ ok: boolean; forgotten: boolean; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}/forget`,
      { method: "POST" },
    );
  },
  createDelegate(entry: Record<string, unknown>) {
    return request<{ ok: boolean; message: string; delegates: DelegateView[] }>("/api/delegates", {
      method: "POST",
      body: entry,
    });
  },
  updateDelegate(name: string, entry: Record<string, unknown>) {
    return request<{ ok: boolean; message: string; delegates: DelegateView[] }>(
      `/api/delegates/${encodeURIComponent(name)}`,
      { method: "PUT", body: entry },
    );
  },
  deleteDelegate(name: string) {
    return request<{ ok: boolean; message: string; delegates: DelegateView[] }>(
      `/api/delegates/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
  },
  testDelegate(entry: Record<string, unknown>) {
    return request<DelegateProbe>("/api/delegates/test", { method: "POST", body: entry });
  },
};
