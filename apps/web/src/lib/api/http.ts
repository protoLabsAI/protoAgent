/**
 * The console's JSON/multipart fetch layer + its error model, split out of `lib/api.ts`
 * (#3808). Must never import `lib/api.ts` (that would be a cycle); `lib/api.ts` re-exports
 * the public names so importers keep using `../lib/api`.
 */
import { notifyAuthRequired } from "../auth";
import { apiUrl, applyAuth, currentSlug, isAgentPath, memberPath } from "./routing";

export type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  /** Pin to the HUB (never slug-route) — for origin-level reads like the tenant uid
   * that must NOT follow the focused agent. */
  host?: boolean;
};

/** An HTTP error from `request()` that carries the status code, so callers (and the
 *  QueryClient's retry policy) can react to it without parsing the message. */
export class ApiError extends Error {
  /** `status` is the HTTP status; `code` is the machine-readable `detail.code` when the
   *  server sent a structured `{detail: {code, reason}}` (e.g. the fs routes, ADR 0112). */
  constructor(readonly status: number, message: string, readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

/** Pull a human message (+ a machine code) out of an error response body. FastAPI's
 *  `detail` is a string for plain HTTPExceptions, but routes that raise a STRUCTURED detail
 *  send `{code, reason}` — and a validation error sends a list. Interpolating either of those
 *  straight into the message produced "[object Object]". */
export function parseErrorBody(raw: string, fallback: string): { detail: string; code?: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { detail: raw || fallback };
  }
  const d = parsed && typeof parsed === "object" ? (parsed as { detail?: unknown }).detail : undefined;
  if (typeof d === "string" && d) return { detail: d };
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const o = d as { code?: unknown; reason?: unknown; message?: unknown };
    const code = typeof o.code === "string" ? o.code : undefined;
    const msg =
      (typeof o.reason === "string" && o.reason) ||
      (typeof o.message === "string" && o.message) ||
      code ||
      fallback;
    return { detail: msg, code };
  }
  if (Array.isArray(d) && d.length) {
    const first = d[0] as { msg?: unknown };
    if (first && typeof first.msg === "string") return { detail: first.msg };
  }
  return { detail: raw || fallback };
}

/** Cold start: the backend isn't answering *yet*, but will be shortly — retry through
 *  it instead of flashing an error. Two shapes:
 *   - HTTP 409 / 502: a just-switched-to fleet agent (the member isn't running yet —
 *     `activate` is still spawning it) or its hub proxy (booting, not bound).
 *   - A fetch that threw before any response (no ApiError status): the LOCAL desktop
 *     sidecar isn't bound to its port yet during the ~12s first-launch boot. WKWebView
 *     surfaces this as `TypeError: Load failed` — which is exactly why the tasks/notes
 *     panels showed "Load failed" and had to be reloaded on a fresh desktop start.
 *  A genuinely-down backend just keeps the panels in their loading state until the
 *  shell's boot-gate ("isn't responding") takes over — same as before. */
export function isColdStart(error: unknown): boolean {
  if (error instanceof ApiError) return error.status === 409 || error.status === 502;
  return true; // no HTTP response at all ⇒ not reachable yet (desktop sidecar booting)
}

/** The fleet proxy's "agent isn't running/registered" signal (ADR 0042): a 409 from a
 *  slug-routed call. Distinct from `isColdStart` (which also rides 502/no-response) — this
 *  is specifically "the focused fleet agent is down", used to offer a return-to-host recovery
 *  once it persists past a normal spawn window instead of the generic "isn't responding" gate. */
export function isAgentNotRunning(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

/** True for a 401 from request() — retrying can't help until the operator supplies
 *  a token (#873); the AuthGate owns recovery. */
export function is401(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** The fleet proxy's "can't reach the member" signal (ADR 0042 §I): a 502 from a
 *  slug-routed call. A REMOTE member never 409s (it isn't a local process the hub can find
 *  "not running") — it 502s when its box is offline or its URL is wrong. Distinct from
 *  `isAgentNotRunning` (409) so the boot gate can offer the same return-to-host recovery for a
 *  dead remote that it does for a down local peer. */
export function isAgentUnreachable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 502;
}

/** A request is MEMBER-scoped when it's slug-routed to the focused agent (not the hub). A 401
 *  from one is that member's credential problem — a wrong/missing stored token for a REMOTE —
 *  NOT the hub's, so it must not trip the global AuthGate (which prompts for, and would
 *  overwrite, the HUB token). `host:true` and the host window are always hub-scoped. Exported
 *  for unit testing (it gates whether a 401 reaches `notifyAuthRequired`). */
export function isMemberScoped(path: string, host?: boolean): boolean {
  return !host && currentSlug() !== "host" && isAgentPath(path);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { host, ...init } = options;  // `host` is ours (routing), not a fetch RequestInit field
  const headers = applyAuth(new Headers(init.headers));
  let body: BodyInit | undefined;
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.body);
  }

  const response = await fetch(apiUrl(path, { host }), {
    ...init,
    headers,
    body,
  });

  if (!response.ok) {
    // Read the body ONCE — calling response.json() then response.text() on the same
    // response throws "body stream already read" (a second error that masks the real
    // one). Read text, then best-effort parse a JSON {detail}.
    const raw = await response.text().catch(() => "");
    const { detail, code } = parseErrorBody(raw, `${response.status} ${response.statusText}`);
    // Wrong/expired/missing bearer on a token-gated deployment — surface the
    // token prompt (#873) instead of leaving per-panel 401 cards as the only signal.
    // But a MEMBER-scoped 401 is the focused remote's bad token, not the hub's — don't
    // hijack the hub AuthGate; the boot gate / fleet panel own that recovery.
    if (response.status === 401 && !isMemberScoped(path, host)) notifyAuthRequired();
    throw new ApiError(response.status, detail || "request failed", code);
  }

  return (await response.json()) as T;
}

// Multipart sibling of `request` for file uploads (the ingestion engine). Never
// sets Content-Type — the browser adds the multipart boundary itself — but reuses
// the same auth + slug routing + 401 handling.
export async function requestForm<T>(path: string, form: FormData, opts: { host?: boolean } = {}): Promise<T> {
  const headers = applyAuth(new Headers());
  const response = await fetch(apiUrl(path, { host: opts.host }), {
    method: "POST",
    headers,
    body: form,
  });
  if (!response.ok) {
    // Read the body ONCE (a Response stream can't be read twice — calling
    // .json() then .text() throws "body stream already read", which masked the
    // real HTTP detail and skipped the 401 AuthGate). Mirror `request`.
    const raw = await response.text().catch(() => "");
    const { detail, code } = parseErrorBody(raw, `${response.status} ${response.statusText}`);
    if (response.status === 401 && !isMemberScoped(path, opts.host)) notifyAuthRequired();
    throw new ApiError(response.status, detail || "request failed", code);
  }
  return (await response.json()) as T;
}

// GET a read for an EXPLICITLY chosen fleet member, via the hub's per-agent proxy
// (`memberPath`) — independent of which window is focused. `request()` slug-routes to the
// CURRENT window; this targets an arbitrary member, which is what the Fleet Room diagnostics
// drawer (#3169) needs: it stays bound to the member the operator picked, not the focused
// agent. The HTTP status is preserved on the thrown `ApiError` so the drawer can map the
// proxy's reachability codes (409 stopped / 502 unreachable / 504 timeout) and the member's
// own 401/404/503 onto actionable inline states.
//
// A member-scoped 401 is that member's credential problem, not the hub's, so — like
// `isMemberScoped` in `request()` — it deliberately does NOT trip the global AuthGate
// (which would prompt for, and overwrite, the HUB token). The drawer surfaces it inline.
export async function memberRequest<T>(slug: string, rel: string): Promise<T> {
  const response = await fetch(memberPath(slug, rel), { headers: applyAuth(new Headers()) });
  if (!response.ok) {
    // Read the body ONCE (a Response stream can't be read twice), best-effort parsing a
    // JSON {detail} — mirrors `request`/`requestForm`.
    const raw = await response.text().catch(() => "");
    const { detail, code } = parseErrorBody(raw, `${response.status} ${response.statusText}`);
    throw new ApiError(response.status, detail || "request failed", code);
  }
  return (await response.json()) as T;
}
