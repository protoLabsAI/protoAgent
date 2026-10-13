import { currentSlug } from "./api/routing";
import { ApiError } from "./api/http";
import { automationApi } from "./api/automation";
import { chatApi } from "./api/chat";
import { fleetApi } from "./api/fleet";
import { knowledgeApi } from "./api/knowledge";
import { pluginsApi } from "./api/plugins";
import { runtimeApi } from "./api/runtime";
import { setupApi } from "./api/setup";
import { telemetryApi } from "./api/telemetry";
import { workspaceApi } from "./api/workspace";

// #3808: the slug-routing, HTTP and A2A-stream layers live in ./api/*. Re-export every name
// that used to be defined here so no importer (or `vi.mock("../lib/api")`) has to change.
export { agentHref, apiUrl, authToken, currentSlug, isHostConsole, memberPath } from "./api/routing";
export {
  ApiError,
  is401,
  isAgentNotRunning,
  isAgentUnreachable,
  isColdStart,
  isMemberScoped,
  parseErrorBody,
} from "./api/http";
export {
  artifactAppends,
  componentFromParts,
  consumedSteersFromParts,
  contextFromParts,
  costFromMeta,
  drainSseBuffer,
  frameIsForeign,
  hitlFromParts,
  replayDurableChatTurn,
  roomReplyFromParts,
  supersededByFromStatus,
  textFromParts,
  type DurableChatSession,
  type DurableChatTurn,
  type TurnStreamHandlers,
} from "./api/a2aStream";
// #3822: the `api` object's methods live in per-domain slices under ./api/*; re-export the
// types and helpers that used to be defined here alongside it.
export { isDesktopWebview } from "./api/desktop";
export type { PairAddress, PairedDevice, PairHost, PairingStart } from "./api/runtime";
export type { FsDiff, FsDiffFile, FsFile, FsStamp } from "./api/workspace";

/** Boot hook (ADR 0042 slug routing → #806): a window opening `/app/agent/<slug>/` ensures
 * its agent is RUNNING — `POST /api/fleet/<name>/activate` resumes a cold agent from its
 * checkpoint and touches it for keep-N-warm LRU. Every slug navigation is a full page load
 * (FleetSwitcher navigates), so this one boot call covers switch, reload and new-window.
 * Fire-and-forget: the shell's queries already retry through the resume window, and any
 * failure (non-fleet backend, unknown slug) just leaves today's behavior. The slug IS the
 * agent's `id`, and activate resolves id-or-name, so this goes straight there — it used to
 * fetch the whole fleet first only to map the id back to a display name. */
export async function activateSlugAgent(): Promise<void> {
  const slug = currentSlug();
  if (slug === "host") return;
  try {
    await api.activateAgent(slug); // hub control-plane path — never slug-scoped
  } catch {
    // best-effort — the proxy 502s + query retries surface a truly unreachable agent
  }
}

/** THE console API object — one identity shared by every importer, `vi.mock("../lib/api")`
 *  and `vi.spyOn(api, …)`. Cross-method orchestration (activateSlugAgent,
 *  loadBackgroundReport) lives in this file and calls through `api.` so spies intercept.
 *  Method names are unique across slices (`api/domains.test.ts` guards it) — a duplicate
 *  would silently shadow in the spread. */
export const api = {
  ...runtimeApi,
  ...telemetryApi,
  ...automationApi,
  ...knowledgeApi,
  ...setupApi,
  ...fleetApi,
  ...chatApi,
  ...pluginsApi,
  ...workspaceApi,
};

/** Full report body for the chat report card → document viewer (ADR 0070 D4).
 *
 *  Fetches the job by id (`GET /api/background/{id}` — the only route that carries the
 *  untruncated result). Falls back to the legacy list-and-filter ONLY on a 404: a
 *  pre-ADR-0070 server has no by-id route (its router answers 404), and on a current
 *  server a 404 means the job row was deleted — which the list fallback resolves to the
 *  same "no longer available" placeholder. Any other failure (401/500/network) is real
 *  and propagates so the viewer shows its error state instead of a misleading placeholder. */
export async function loadBackgroundReport(jobId: string): Promise<string> {
  const gone =
    "_The full report is no longer available — it may have been cleared from the Background agents panel._";
  try {
    return (await api.backgroundJob(jobId)).result || gone;
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) throw err;
    // Old server (no by-id route) or deleted row — the list answers both.
    const listed = await api.background().catch(() => null);
    return listed?.jobs.find((j) => j.id === jobId)?.result || gone;
  }
}
