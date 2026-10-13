/**
 * The fleet control plane (ADR 0042): roster, archetypes, lifecycle, remotes, broadcast and
 * member diagnostics. Hub-scoped paths (`isHubPath`) never slug-route.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  Archetype,
  ArchetypeFromUrl,
  ArchetypePreview,
  DiagnosticsLogs,
  DiagnosticsTask,
  DiscoveredAgent,
  FleetAgent,
  RemoteAuth,
  FleetStatus,
} from "../types";
import { memberPath, applyAuth } from "./routing";
import { request, memberRequest } from "./http";

export const fleetApi = {
  // --- Fleet (ADR 0042) — many workspace agents on one host ------------------
  fleet() {
    return request<FleetStatus>("/api/fleet");
  },
  // Persist the fleet roster's DISPLAY order (ADR 0042 hub control-plane; backend from
  // PR #3200, issue #3197). `order` is the COMPLETE permutation of the live roster
  // (host + local + remote) by immutable FleetAgent.id — stable ids, NEVER an editable
  // name/label, so a rename can't perturb order. The path lives under /api/fleet, so
  // `isHubPath` keeps it HUB-scoped (never slug-routed) like every other fleet control
  // call. The server validates the permutation under its state lock and 400s a duplicate /
  // unknown / missing / malformed list WITHOUT touching the saved order; on success it
  // echoes the applied order, and a subsequent GET /api/fleet returns members in it,
  // reconciled as members are later added or removed.
  reorderFleet(order: string[]) {
    return request<{ ok: boolean; order: string[] }>("/api/fleet/order", {
      method: "PUT",
      body: { order },
    });
  },
  discoverAgents() {
    return request<{ discovered: DiscoveredAgent[] }>("/api/fleet/discover");
  },
  // `includeHeld` asks for the catalog's held (preview) archetypes too — only the New-agent
  // picker's opt-in sends it; they're never part of the default list.
  archetypes(includeHeld = false) {
    return request<{ archetypes: Archetype[] }>(includeHeld ? "/api/archetypes?include_held=1" : "/api/archetypes");
  },
  archetypePreview(id: string) {
    return request<ArchetypePreview>(`/api/archetypes/${encodeURIComponent(id)}/preview`);
  },
  /** Peek an uncatalogued bundle by git URL (+ optional ref) — read-only, nothing installs.
   *  400 = not a git URL / bad ref; 502 = the repo couldn't be read. */
  archetypeFromUrl(url: string, ref?: string) {
    const q = new URLSearchParams({ url });
    if (ref) q.set("ref", ref);
    return request<ArchetypeFromUrl>(`/api/archetypes/from-url?${q.toString()}`);
  },
  createAgent(body: {
    name: string;
    bundle?: string | null;
    // Tag / branch / SHA to install `bundle` at ("From a bundle URL"); omitted = default branch.
    ref?: string;
    soul?: string;
    port?: number;
    start?: boolean;
    shared_skills?: boolean;
    // Operator-supplied bundle-seed values (#2041): `inputs` fill the bundle's MCP
    // `${input}` placeholders (an entry seeds ENABLED when its required inputs are here),
    // `secrets` carry values for the bundle's declared secrets. Omitted → env-only.
    inputs?: Record<string, string>;
    secrets?: { key: string; value: string }[];
    // Answers to the bundle's declared config_inputs prompts (#2934), keyed by dotted
    // config path — written into the member's config at those keys. Omitted → defaults.
    config_inputs?: Record<string, string | boolean>;
    // The picked archetype's capability contract (#2277), persisted to the member's
    // workspace.yaml so it can warn at boot when its toolset doesn't cover the persona.
    requires_tools?: string[];
  }) {
    return request<{ ok: boolean; agent: FleetAgent; installed: string[] }>("/api/fleet", {
      method: "POST",
      body,
    });
  },
  // The fleet control plane resolves an agent by `id` OR display `name`, id first. Callers
  // should pass the **id**: display names are editable — a member can even rename itself from
  // its own Identity panel, where sibling names aren't knowable — so only the id is unique.
  startAgent(ident: string) {
    return request<{ ok: boolean; agent: FleetAgent }>(`/api/fleet/${encodeURIComponent(ident)}/start`, {
      method: "POST",
    });
  },
  stopAgent(ident: string) {
    return request<{ ok: boolean; name: string; stopped: boolean }>(`/api/fleet/${encodeURIComponent(ident)}/stop`, {
      method: "POST",
    });
  },
  /** Pair this hub with a remote protoAgent (ADR 0113 D1): the HUB's server redeems a code
   *  minted on the remote (its Settings ▸ Devices ▸ Pair an agent, or `protoagent pair`) and
   *  stores the per-device token it gets back — the token never reaches this browser. Adds the
   *  member, or re-tokens it in place when that URL is already one (`action: "retokened"` —
   *  the Re-pair path; id, slug and open windows survive). Errors carry the server's `detail`
   *  (400: bad url/name, invalid/expired code; 502: unreachable or not a pairing protoAgent). */
  pairRemote(body: { url: string; code: string; name?: string; allow_insecure?: boolean }) {
    return request<{
      ok: boolean;
      agent: FleetAgent;
      reachable?: boolean;
      version?: string;
      auth?: RemoteAuth;
      action?: "added" | "retokened";
    }>("/api/fleet/remotes/pair", { method: "POST", body });
  },
  addRemoteAgent(body: { name: string; url: string; token?: string; allow_insecure?: boolean }) {
    // Register a remote protoAgent as a SWITCHABLE fleet member (ADR 0042 §I) —
    // it gets a slug window; the hub reverse-proxies its console + A2A. The server
    // probes it at register time and returns `reachable`/`version` so the caller can
    // warn up front (registration is NOT rejected for an unreachable peer — deferred
    // registration is intentional; a peer can come online later).
    return request<{ ok: boolean; agent: FleetAgent; reachable?: boolean; version?: string; auth?: RemoteAuth }>("/api/fleet/remotes", {
      method: "POST",
      body,
    });
  },
  updateRemoteAgent(ident: string, body: { name?: string; url?: string; token?: string; allow_insecure?: boolean }) {
    // Edit a remote member in place (ADR 0042 §I) — omitted fields keep their value;
    // token:"" clears the stored bearer. The id/slug is unchanged, so open windows survive.
    // The server re-probes and returns fresh {reachable, version, auth}. A url on a NEW origin
    // with no token in the same body clears the stored token (it was issued by the old host)
    // and the answer says `token_cleared: true` (ADR 0113).
    return request<{
      ok: boolean;
      agent: FleetAgent;
      reachable?: boolean;
      version?: string;
      auth?: RemoteAuth;
      token_cleared?: boolean;
    }>(
      `/api/fleet/remotes/${encodeURIComponent(ident)}`,
      { method: "PATCH", body },
    );
  },
  removeRemoteAgent(ident: string) {
    return request<{ ok: boolean; id: string; name: string }>(`/api/fleet/remotes/${encodeURIComponent(ident)}`, {
      method: "DELETE",
    });
  },
  renameAgent(ident: string, name: string) {
    // Display rename only — the id (URL slug + data scope) is immutable.
    return request<{ ok: boolean; id: string; name: string }>(`/api/fleet/${encodeURIComponent(ident)}`, {
      method: "PATCH",
      body: { name },
    });
  },
  removeAgent(ident: string, purge = false) {
    return request<{ ok: boolean; name: string; removed: string[] }>(
      `/api/fleet/${encodeURIComponent(ident)}${purge ? "?purge=true" : ""}`,
      { method: "DELETE" },
    );
  },
  activateAgent(ident: string) {
    // #806: ensure-running + keep-N-warm touch (no server-side active pointer since slug routing).
    return request<{ ok: boolean; evicted: string[] }>(`/api/fleet/${encodeURIComponent(ident)}/activate`, {
      method: "POST",
    });
  },
  fleetDown() {
    return request<{ ok: boolean; stopped: string[] }>("/api/fleet/down", { method: "POST" });
  },
  // Fire a one-shot message at a specific fleet member (Fleet Room broadcast). Goes to the
  // member's A2A endpoint through the hub proxy (/agents/<slug>/a2a) — NOT /api/chat —
  // because the streaming A2A turn publishes `turn.usage` to the member's event bus, which
  // is what surfaces "<member> finished a turn" in the activity feed (the non-streaming
  // /api/chat path publishes nothing). Fire-and-forget: the A2A task is durable, so we send
  // the request, then cancel the response stream — the turn keeps running server-side and
  // emits its bus event. `slug` is the member id, "host" for this instance.
  sendToAgent(slug: string, message: string, sessionId?: string): Promise<void> {
    const rpcId = `flr-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const body = {
      jsonrpc: "2.0",
      id: rpcId,
      method: "SendStreamingMessage",
      params: {
        message: {
          role: "ROLE_USER",
          parts: [{ text: message }],
          messageId: rpcId,
          contextId: sessionId ?? `fleet-room-${Date.now()}`,
        },
      },
    };
    return fetch(memberPath(slug, "/a2a"), {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      body: JSON.stringify(body),
    }).then((res) => {
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      void res.body?.cancel().catch(() => {}); // durable task runs on + emits turn.usage
    });
  },

  // Member diagnostics (#3168) — bounded, redacted, read-only reads for an EXPLICITLY chosen
  // member (`slug` = its id, or "host" for this instance), reached through the hub's
  // /agents/<slug>/* proxy regardless of the focused window. The Fleet Room drawer (#3169)
  // drives these; the server owns the caps + redaction, so the client only presents the
  // returned snapshot. Snapshot reads — no live SSE following of the log stream.
  memberDiagnosticsLogs(slug: string, lines?: number): Promise<DiagnosticsLogs> {
    // `lines` is left to the server default when omitted; an out-of-range value is CLAMPED
    // (never a 422) and reported back on `note`, so the drawer can always render an answer.
    const q = typeof lines === "number" ? `?lines=${encodeURIComponent(String(lines))}` : "";
    return memberRequest<DiagnosticsLogs>(slug, `/api/diagnostics/logs${q}`);
  },
  memberDiagnosticsTask(slug: string, taskId: string): Promise<DiagnosticsTask> {
    return memberRequest<DiagnosticsTask>(slug, `/api/diagnostics/tasks/${encodeURIComponent(taskId)}`);
  },
};
