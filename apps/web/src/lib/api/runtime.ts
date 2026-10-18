/**
 * The host process: runtime status, device pairing (ADR 0087/0113), SSE tokens, restart,
 * the managed Node/Python runtimes (ADR 0085/0094), and the desktop in-app updater (Tauri).
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type { NodeRuntimePayload, PythonRuntimePayload, RuntimeStatus } from "../types";
import { cancelBody, type PairKind } from "../agentPairing";
import { apiUrl, applyAuth } from "./routing";
import { request } from "./http";
import { tauriCore } from "./desktop";

/** A reachable address in a pairing-start answer. For a PHONE code `url` is the full
 *  `…/app/#pair=<code>` link, `qr` its rendered SVG, and `base_url` the bare console URL a
 *  phone that can't scan opens before TYPING the same code (ADR 0087 D3 amendment); for an
 *  AGENT code (ADR 0113) `url` is the bare base URL a hub pairs against and there is no QR. */
export type PairHost = {
  host: string;
  kind: "tailnet" | "lan";
  url: string;
  qr?: string | null;
  base_url?: string;
  /** How the address reaches this agent (#4336 S4): `serve` = tailnet HTTPS through
   *  `tailscale serve`, `magicdns` = the tailnet NAME over http, `ip` = a raw address.
   *  Absent from older servers (read as `ip`). */
  via?: "serve" | "magicdns" | "ip";
  /** https — a secure context on the phone. */
  secure?: boolean;
  /** The serve handler is ALSO public through Tailscale Funnel. */
  funnel?: boolean;
  /** scheme://host[:port] of this address. */
  origin?: string;
};
export type PairAddress = { host: string; kind: "tailnet" | "lan" };

/** `tailscale status` as the server sees it (infra/tailscale.py). */
export type TailscaleInfo = {
  available: boolean;
  running: boolean;
  dns_name: string;
  magicdns: boolean;
  https: boolean;
  ips: string[];
  error: string;
  served: { url: string; https_port: number; funnel: boolean } | null;
  https_admin_url: string;
};
/** Whether the panel can restart this agent so a new bind applies, and what to say if not. */
export type RestartInfo = { supported: boolean; bind_pinned: string; container: boolean; hint: string; pending: boolean };
/** How this agent can be reached (#4336 S4) — carried on every pairing-start answer. */
export type PairReach = {
  tailscale: TailscaleInfo;
  restart: RestartInfo;
  auth_configured: boolean;
  /** This server listens on a picked interface AND loopback, so binding the picked IP is safe. */
  bind_interface?: boolean;
};
export type ServeResult = { ok: boolean; url?: string; reason?: string; error?: string; enable_url?: string };
/** A row of `GET /api/devices`. `kind` says whether the client is a phone/tablet or another
 *  agent's hub (ADR 0113 D3) — fixed by the code it claimed; absent (pre-0113) reads `device`. */
export type PairedDevice = {
  id: string;
  name: string;
  created_at: number;
  last_seen_at: number | null;
  kind?: PairKind;
  /** Server-computed against its own last-seen write throttle (#4336 S3), so the badge can't
   *  drift from how often the stamp is refreshed. Absent from older servers. */
  active?: boolean;
};
export type PairingStart =
  | {
      ok: true;
      /** Which code this is — echoed by the server; absent from pre-ADR-0113 servers (a phone code). */
      kind?: PairKind;
      /** A phone code is 32 url-safe chars; an agent code is `XXXXX-XXXXX`. */
      code: string;
      expires_at: number;
      ttl: number;
      hosts: PairHost[];
      /** Agent codes only: this agent's display name, so the hint can say who's pairing. */
      name?: string;
      reach?: PairReach;
    }
  /** Nothing reachable — `available` is what the server COULD bind to (ADR 0087 D6).
   *  `authConfigured` is THIS SERVER's answer to "do I have a token", which a client cannot
   *  infer: localStorage holding one says nothing about what the server accepts. */
  | {
      ok: false;
      error: string;
      available: PairAddress[];
      bind: string;
      authConfigured: boolean;
      /** `no_console`: a phone code on a `--ui none` instance (nothing for a phone to open). */
      reason?: string;
      reach?: PairReach;
    };

export const runtimeApi = {
  runtimeStatus() {
    return request<RuntimeStatus>("/api/runtime/status");
  },

  // ── Device pairing (ADR 0087) ──
  // The CLAIM half deliberately lives outside this object (lib/pairing.ts): `request`
  // attaches the operator bearer, and claiming runs precisely when there isn't one.
  devices() {
    return request<{ devices: PairedDevice[] }>("/api/devices");
  },
  /** Start pairing.
   *
   * Read directly rather than through `request` because the 409 body is MEANINGFUL: a
   * loopback-bound instance reports the addresses it *could* be reached on so the panel can
   * offer to bind there. `request` collapses every non-2xx into a thrown string, which would
   * throw that payload away — and the alternative (returning 200 with ok:false) would weaken
   * a correct status code for client convenience. */
  async pairingStart(kind: PairKind = "device"): Promise<PairingStart> {
    const headers = applyAuth(new Headers());
    headers.set("Content-Type", "application/json");
    const res = await fetch(apiUrl("/api/pairing/start"), {
      method: "POST",
      headers,
      // `kind` is fixed at MINT time (ADR 0113 D2) — the claimer can't relabel a phone code
      // as an agent, so this body is the only place the kind is ever chosen.
      body: JSON.stringify({ kind }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) return { ok: true, ...data } as PairingStart;
    return {
      ok: false,
      error: String(data?.error || `${res.status} ${res.statusText}`),
      available: Array.isArray(data?.available) ? data.available : [],
      bind: String(data?.bind || ""),
      // Absent (an older server) is treated as NOT configured, so the flow mints rather than
      // assuming — minting a second token is recoverable; writing a bind with no token is not.
      authConfigured: data?.auth_configured === true,
      ...(typeof data?.reason === "string" ? { reason: data.reason } : {}),
      ...(data?.reach && typeof data.reach === "object" ? { reach: data.reach as PairReach } : {}),
    };
  },
  /** Serve this agent over tailnet HTTPS (`tailscale serve`), or stop. Read directly: the
   *  409 body (`no_token`, `https_disabled` + `enable_url`, …) is what the panel shows. */
  async tailscaleServe(on: boolean): Promise<ServeResult> {
    const headers = applyAuth(new Headers());
    const res = await fetch(apiUrl("/api/pairing/tailscale/serve"), { method: on ? "POST" : "DELETE", headers });
    const data = await res.json().catch(() => ({}));
    return {
      ok: res.ok && data?.ok === true,
      ...(typeof data?.url === "string" ? { url: data.url } : {}),
      ...(typeof data?.reason === "string" ? { reason: data.reason } : {}),
      error: typeof data?.error === "string" ? data.error : res.ok ? undefined : `${res.status} ${res.statusText}`,
      ...(typeof data?.enable_url === "string" ? { enable_url: data.enable_url } : {}),
    };
  },
  /** Whether the server answers at all — `/healthz` is public, so this works mid-restart
   *  and before the boot gate has a token. */
  async serverUp(): Promise<boolean> {
    try {
      const res = await fetch(apiUrl("/healthz", { host: true }), { cache: "no-store" });
      return res.ok;
    } catch {
      return false;
    }
  },
  /** Drop this dialog's pending code. Scoped by kind (see `cancelBody`): an unscoped cancel
   *  would also kill a code of the OTHER kind that another dialog is still showing. */
  pairingCancel(kind: PairKind) {
    return request<{ ok: boolean }>("/api/pairing/cancel", { method: "POST", body: cancelBody(kind) });
  },
  /** Rename a paired device (#4336 S3). The server validates (1–64 printable chars) and
   *  answers a refusal with a `detail` the panel shows as-is. */
  renameDevice(id: string, name: string) {
    return request<{ ok: boolean; device: PairedDevice }>(`/api/devices/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: { name },
    });
  },
  revokeDevice(id: string) {
    return request<{ ok: boolean }>(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Short-lived HMAC token for the SSE EventSource, which can't send an
  // Authorization header. Bearer-gated; in open mode the server returns "" and
  // accepts a tokenless /api/events. events.ts fetches this before each
  // (re)connect.
  //
  // Signed by the HUB (host:true), NOT slug-routed. The proxied `/api/events`
  // is validated at the HUB's auth middleware FIRST (before it forwards to the
  // member), so the token must carry the hub's signature or the stream 401s at
  // the hub for every non-host member on a bearer-gated hub. The hub then
  // forwards with the member's own credential attached, so the member accepts it
  // downstream (its SSE branch falls through to the bearer check). That holds for
  // EVERY proxied stream — the Fleet Room's per-member feeds included (#4292): a
  // token minted BY the member (`/agents/<slug>/api/sse-token`) carries the member's
  // signature, which the hub can never verify.
  sseToken() {
    return request<{ token: string }>("/api/sse-token", { host: true });
  },

  // Gracefully restart the server process (POST /api/restart) — the server drains and
  // re-execs; the console reconnects via the boot gate. Always targets the HOST (the
  // process you're connected to), never a slug-routed agent.
  restart() {
    return request<{ ok: boolean; restarting: boolean }>("/api/restart", { method: "POST", host: true });
  },

  // Managed Node runtime (ADR 0085) — status + one-click provisioning of node/npx for
  // the npx-based ACP agents + MCP servers. HOST-targeted (the box-shared runtime lives
  // on the server process, not a slug-routed agent), like restart().
  nodeRuntime() {
    return request<NodeRuntimePayload>("/api/runtime/node", { host: true });
  },
  installNodeRuntime(force = false) {
    return request<{ ok: boolean } & NodeRuntimePayload>(
      `/api/runtime/node/install${force ? "?force=true" : ""}`,
      { method: "POST", host: true },
    );
  },

  // Managed Python runtime (ADR 0094) — status + one-click provisioning of the
  // execute_code child interpreter for the packaged desktop app. HOST-targeted like
  // nodeRuntime(): the box-shared runtime lives on the server process.
  pythonRuntime() {
    return request<PythonRuntimePayload>("/api/runtime/python", { host: true });
  },
  installPythonRuntime(force = false) {
    return request<{ ok: boolean } & PythonRuntimePayload>(
      `/api/runtime/python/install${force ? "?force=true" : ""}`,
      { method: "POST", host: true },
    );
  },

  // The HUB's runtime status — NEVER slug-routed. The TenantGuard keys on the hub's
  // `instance_uid` (the real tenant of this origin), which is STABLE across agent
  // swaps. The slug-routed runtimeStatus() returns the FOCUSED agent's uid, which
  // changes on every switch and would wrongly wipe the chat view each time.
  hostRuntimeStatus() {
    return request<RuntimeStatus>("/api/runtime/status", { host: true });
  },

  /** Desktop in-app updater (Tauri). `checkUpdate` returns the available build's
   * version + notes (the changelog from latest.json) or null (up to date / not
   * desktop). Updater failures reject so an explicit tray request can surface them;
   * ambient callers decide whether to stay quiet. */
  async checkUpdate(): Promise<{ version: string; current: string; notes: string } | null> {
    const core = tauriCore();
    if (!core) return null;
    return (await core.invoke<{ version: string; current: string; notes: string } | null>("updater_check")) ?? null;
  },
  /** The LAUNCH check's stored outcome (#2203) — the shell runs one update check in
   * parallel with engine startup; this pulls its result (a state read, no network).
   * `done: false` while the check is still in flight. Null outside the shell or on an
   * older shell without the command — callers must treat null as "no launch check". */
  async launchUpdateResult(): Promise<{
    done: boolean;
    update: { version: string; current: string; notes: string } | null;
  } | null> {
    const core = tauriCore();
    if (!core) return null;
    try {
      return (
        (await core.invoke<{ done: boolean; update: { version: string; current: string; notes: string } | null }>(
          "updater_launch_result",
        )) ?? null
      );
    } catch {
      return null; // older shell without the command — UpdateNotice falls back to its timers
    }
  },
  /** Destructively consume the latest tray request retained by Rust across the
   * webview's boot/listener race. Only the primary `main` window can receive it. */
  async consumeUpdateRequest(): Promise<number | null> {
    const core = tauriCore();
    if (!core) return null;
    try {
      return (await core.invoke<number | null>("updater_consume_request")) ?? null;
    } catch {
      return null; // older shell without the durable request command
    }
  },
  async ackUpdateRequest(requestId: number): Promise<void> {
    const core = tauriCore();
    if (!core) return;
    try {
      await core.invoke("updater_ack_request", { requestId });
    } catch {
      // Older shell: the request may replay after a webview reload, but id de-duping
      // still prevents duplicates within this mount.
    }
  },
  async installUpdate(
    expectedVersion: string,
    onProgress: (e: { chunkLength: number; contentLength: number | null }) => void,
  ): Promise<
    | { status: "superseded"; update: { version: string; current: string; notes: string } }
    | { status: "upToDate" }
  > {
    const core = tauriCore();
    if (!core) throw new Error("Tauri core API unavailable");
    const channel = new core.Channel<{ chunkLength: number; contentLength: number | null }>();
    channel.onmessage = onProgress;
    // Resolves only if install fails — on success the Rust command relaunches the app.
    return await core.invoke<
      | { status: "superseded"; update: { version: string; current: string; notes: string } }
      | { status: "upToDate" }
    >("updater_install", { expectedVersion, onProgress: channel });
  },
};
