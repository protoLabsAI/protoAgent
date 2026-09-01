// The Fleet Room (ADR 0042 + the palette-UX overhaul). A native palette morph-view
// that makes the fleet feel like a Discord room: a roster of presence-aware MEMBERS on the
// left, the live fleet activity feed on the right, a broadcast bar below. Click a member
// to DM it — that's the wired palette chat (PaletteChat) pointed at the member
// (`ctx.enter("member-dm", …)`), streaming through the hub proxy, with Back to the roster.
// The bottom bar broadcasts to everyone online (the @everyone announce — the only
// fire-and-forget path, since you can't stream N replies into one pane).
//
// Entered from the palette's "Agents" group; the DS CommandPalette supplies the back/close
// chrome + footer. Open in every sister agent's window as well as the host's: `/api/fleet` is
// a hub path (never slug-scoped), so the roster it reflects is the same one from anywhere.
// The one window it's withheld from is a member reached DIRECTLY on its own port, where the
// fleet really is a fleet-of-one — see fleetSettingsGate.
import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { Activity, ArrowLeft, ExternalLink, Play, Radio, RefreshCw, ScrollText, Search, Send, Square } from "lucide-react";
import { useToast } from "@protolabsai/ui/overlays";
import type { PaletteContext, PaletteView } from "@protolabsai/ui/command-palette";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import { ApiError, api, currentSlug } from "../lib/api";
import { fleetQuery, queryKeys } from "../lib/queries";
import { errMsg, localStamp } from "../lib/format";
import type { DiagnosticsLogs, DiagnosticsTask, FleetAgent } from "../lib/types";
import {
  FleetActivityFeed,
  markMemberDone,
  markMemberRunning,
  pushFleetEvent,
  useMemberAwaiting,
  useMemberRunning,
} from "./FleetActivity";
import "./fleet-room.css";

/** The routing slug for a member — the host entry is the reserved "host" (ADR 0042). */
const slugOf = (a: FleetAgent): string => (a.host ? "host" : a.id);

type PresenceKey = "host" | "online" | "remote" | "stopped" | "unreachable";

/** Presence derived from the roster: `running` IS the live reachability probe, `remote`
 *  distinguishes a proxied peer, `host` is the instance serving this console. */
function presenceOf(a: FleetAgent): { key: PresenceKey; label: string } {
  if (a.host) return { key: "host", label: "this instance" };
  if (a.running) return a.remote ? { key: "remote", label: "remote" } : { key: "online", label: "online" };
  return a.remote ? { key: "unreachable", label: "unreachable" } : { key: "stopped", label: "stopped" };
}

const clip = (s: string, n = 72): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

function FleetRoom({ ctx, onOpenAgent }: { ctx: PaletteContext; onOpenAgent: (slug: string) => void }) {
  const { data: fleet } = useQuery(fleetQuery());
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState("");
  const [target, setTarget] = useState<"broadcast" | string>("broadcast");
  // The Diagnostics drawer's inspected member is DRAWER-LOCAL and PINNED at open time (#3169,
  // ADR 0042): it stays authoritative across the 3s fleet poll and every selection change, and
  // never retargets to the composer `target` or the focused window. null ⇒ the drawer is closed.
  const [diag, setDiag] = useState<{ slug: string; name: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const here = currentSlug();

  useEffect(() => inputRef.current?.focus(), []);

  // Host first, then reachable (running) before down, then alphabetical — deterministic
  // across the 3s poll (React Query structural-shares equal data, so no reorder churn).
  const roster = useMemo(() => {
    const agents = fleet?.agents ?? [];
    return [...agents].sort(
      (a, b) =>
        Number(!!b.host) - Number(!!a.host) ||
        Number(b.running) - Number(a.running) ||
        a.name.localeCompare(b.name),
    );
  }, [fleet]);

  // Broadcast reaches every OTHER online member (never the window you're already in).
  const broadcastTargets = useMemo(
    () => roster.filter((a) => a.running && slugOf(a) !== here),
    [roster, here],
  );
  const onlineCount = roster.filter((a) => a.running).length;
  const running = useMemberRunning();
  const awaiting = useMemberAwaiting();

  // DM a member = the wired chat, retargeted. Push it on the palette stack so Back/Escape
  // return here. Only running members are reachable.
  const dm = (a: FleetAgent) => {
    if (!a.running) return;
    ctx.enter("member-dm", { slug: slugOf(a), name: a.name });
  };

  const open = (a: FleetAgent) => {
    ctx.close();
    onOpenAgent(slugOf(a)); // routed through the palette nav chokepoint (launcher-safe)
  };

  // Open the diagnostics drawer on the EXPLICITLY clicked member, pinning its slug + name so
  // the drawer keeps identifying it even after the roster re-sorts or the operator addresses a
  // different member below. Available on every row (incl. stopped/remote): the drawer renders
  // the actionable stopped/unreachable state rather than hiding the affordance.
  const openDiag = (a: FleetAgent) => setDiag({ slug: slugOf(a), name: a.name });

  const toggle = (a: FleetAgent) => {
    const on = a.running;
    // By id, not display name — names are editable (and a member can rename itself), so
    // only the id is guaranteed to address the agent the operator clicked.
    (on ? api.stopAgent(a.id) : api.startAgent(a.id))
      .then(() => {
        qc.invalidateQueries({ queryKey: queryKeys.fleet });
        toast({
          tone: "success",
          title: on ? `Stopping ${a.name}…` : `Starting ${a.name}…`,
          message: on ? `${a.name} is going offline.` : `${a.name} is coming online.`,
        });
      })
      .catch((e) => toast({ tone: "error", title: "Couldn't toggle agent", message: errMsg(e) }));
  };

  const broadcast = (msg: string) => {
    if (!msg) return;
    if (!broadcastTargets.length) {
      toast({ tone: "error", title: "No one to broadcast to", message: "No other members are online." });
      return;
    }
    // Fire-and-forget fan-out — each member runs the turn durably on its own instance.
    // Mark each busy now (optimistic "running" pill); its terminal turn.usage clears it.
    for (const a of broadcastTargets) {
      markMemberRunning(slugOf(a));
      api.sendToAgent(slugOf(a), msg).catch((e) => {
        markMemberDone(slugOf(a)); // the send failed → no turn will run to clear the pill
        toast({ tone: "error", title: `Couldn't reach ${a.name}`, message: errMsg(e) });
      });
    }
    toast({
      tone: "success",
      title: `Broadcast to ${broadcastTargets.length} member${broadcastTargets.length > 1 ? "s" : ""}`,
      message: clip(msg),
    });
    pushFleetEvent({ source: "you", text: `broadcast to ${broadcastTargets.length}: “${clip(msg, 48)}”`, kind: "broadcast" });
    setDraft("");
    inputRef.current?.focus();
  };

  // @-mention in the composer: a trailing "@token" opens a member picker; picking sets the
  // address target (a chip) and strips the token. No target chip = broadcast to all online.
  const mention = (() => {
    const m = draft.match(/(?:^|\s)@([\w-]*)$/);
    return m ? m[1].toLowerCase() : null;
  })();
  const mentionMatches =
    mention !== null
      ? roster.filter((a) => slugOf(a) !== here && a.name.toLowerCase().includes(mention)).slice(0, 6)
      : [];
  const pickMention = (a: FleetAgent) => {
    setTarget(slugOf(a));
    setDraft((d) => d.replace(/(?:^|\s)@[\w-]*$/, ""));
    inputRef.current?.focus();
  };

  const targetAgent = target === "broadcast" ? undefined : roster.find((a) => slugOf(a) === target);

  // Send: an addressed member opens its DM with the message pre-sent (the wired chat streams
  // the reply); otherwise broadcast to all online. ⌘↵ always broadcasts.
  /** Resolve a typed leading "@name" even when the picker was never used — people type
   *  "@scout do the thing" and expect it to address scout, not broadcast it verbatim.
   *  Returns the member plus the message with the mention stripped. */
  const resolveTypedMention = (text: string): { agent?: FleetAgent; rest: string } => {
    const m = text.match(/^\s*@([\w-]+)[\s,:]*/);
    if (!m) return { rest: text };
    const typed = m[1].toLowerCase();
    const others = roster.filter((a) => slugOf(a) !== here);
    const agent =
      others.find((a) => a.name.toLowerCase() === typed) ??
      others.find((a) => a.name.toLowerCase().startsWith(typed));
    return agent ? { agent, rest: text.slice(m[0].length) } : { rest: text };
  };

  const submit = (forceBroadcast: boolean) => {
    const raw = draft.trim();
    if (!raw) return;

    if (!forceBroadcast) {
      // An explicit chip wins; otherwise honor a typed "@name".
      if (!targetAgent && target !== "broadcast") {
        toast({ tone: "error", title: "That member left the fleet", message: "Pick another, or broadcast." });
        setTarget("broadcast");
        return;
      }
      let addressed = targetAgent;
      let msg = raw;
      if (!addressed) {
        const r = resolveTypedMention(raw);
        if (r.agent) {
          addressed = r.agent;
          msg = r.rest.trim();
        }
      }
      if (addressed) {
        if (!addressed.running) {
          toast({ tone: "error", title: `${addressed.name} is offline`, message: "Start it first, or broadcast." });
          return;
        }
        if (!msg) {
          toast({ tone: "error", title: `Add a message for @${addressed.name}`, message: "e.g. “@name ship it”." });
          return;
        }
        ctx.enter("member-dm", { slug: slugOf(addressed), name: addressed.name, initial: msg });
        return;
      }
    }
    broadcast(raw);
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (mentionMatches.length && (e.key === "Enter" || e.key === "Tab")) {
      e.preventDefault();
      pickMention(mentionMatches[0]);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      submit(e.metaKey || e.ctrlKey);
    }
  };

  // Live presence for the PINNED diagnostics member — only the dot/label track the roster;
  // the identity (slug + name) stays fixed from open time. A member that left the fleet
  // degrades to a "gone" label instead of vanishing the drawer.
  const diagMember = diag ? roster.find((a) => slugOf(a) === diag.slug) : undefined;
  const diagPresence = diagMember
    ? presenceOf(diagMember)
    : { key: "stopped" as PresenceKey, label: "left the fleet" };

  return (
    <div className="flr">
      <div className="flr__cols">
        <div className="flr__col flr__roster">
          <div className="flr__colhead">
            <h2>Members</h2>
            <span className="flr__count">
              {onlineCount} online · {roster.length}
            </span>
          </div>
          <div className="flr__list" role="group" aria-label="Fleet members">
            {roster.length === 0 && (
              <div className="flr__empty">No members yet — add one from Settings ▸ Agents.</div>
            )}
            {roster.map((a) => {
              const slug = slugOf(a);
              const p = presenceOf(a);
              // Only a local process can be started/stopped here — and never the agent serving
              // THIS window (`here`): in a sister agent's room its own row would otherwise offer
              // a Stop that kills the console you're standing in. The host row is already
              // excluded on the host console; `here` is what covers a member's window.
              const local = !a.host && !a.remote && slug !== here;
              return (
                <div key={slug} className={`flr__member${a.running ? "" : " is-down"}`}>
                  <span className={`flr__dot flr__dot--${p.key}`} aria-hidden />
                  <button
                    type="button"
                    className="flr__who"
                    onClick={() => dm(a)}
                    disabled={!a.running}
                    title={a.running ? `Message ${a.name}` : `${a.name} is offline`}
                  >
                    <span className="flr__name">
                      {a.name}
                      {a.host && <span className="flr__tag flr__tag--host">this instance</span>}
                      {a.remote && <span className="flr__tag flr__tag--remote">remote</span>}
                    </span>
                    <span className="flr__meta">
                      {[a.bundle, a.port ? `:${a.port}` : null, p.label].filter(Boolean).join(" · ")}
                    </span>
                  </button>
                  <div className="flr__actions">
                    {awaiting[slug] && a.running ? (
                      <span className="flr__pill flr__pill--attn" title="A turn is parked awaiting your answer">
                        needs approval
                      </span>
                    ) : running[slug] && a.running ? (
                      <span className="flr__pill flr__pill--run" title="A turn is in flight">
                        running
                      </span>
                    ) : null}
                    <button
                      type="button"
                      className="flr__icon"
                      onClick={() => openDiag(a)}
                      title={`Diagnostics for ${a.name}`}
                      aria-label={`Diagnostics for ${a.name}`}
                    >
                      <Activity size={14} />
                    </button>
                    {local && (
                      <button
                        type="button"
                        className="flr__icon"
                        onClick={() => toggle(a)}
                        title={a.running ? "Stop" : "Start"}
                        aria-label={a.running ? `Stop ${a.name}` : `Start ${a.name}`}
                      >
                        {a.running ? <Square size={14} /> : <Play size={14} />}
                      </button>
                    )}
                    <button
                      type="button"
                      className="flr__icon"
                      onClick={() => open(a)}
                      disabled={!a.running}
                      title={a.running ? "Open full console" : "Offline"}
                      aria-label={`Open ${a.name} console`}
                    >
                      <ExternalLink size={14} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="flr__col flr__activity">
          <FleetActivityFeed />
        </div>
      </div>

      <div className="flr__composer">
        {mentionMatches.length > 0 && (
          <div className="flr__mentions" role="listbox" aria-label="Address a member">
            {mentionMatches.map((a) => {
              const mp = presenceOf(a);
              return (
                <button
                  key={slugOf(a)}
                  type="button"
                  className="flr__mention"
                  onMouseDown={(e) => {
                    e.preventDefault(); // keep input focus; fire before blur
                    pickMention(a);
                  }}
                >
                  <span className={`flr__dot flr__dot--${mp.key}`} aria-hidden />
                  <span className="flr__mention-name">{a.name}</span>
                  <span className="flr__mention-meta">{mp.label}</span>
                </button>
              );
            })}
          </div>
        )}
        <button
          type="button"
          className={`flr__target${targetAgent ? "" : " is-cast"}`}
          onClick={() => setTarget("broadcast")}
          title={
            targetAgent
              ? `Messaging ${targetAgent.name} — click to broadcast instead`
              : "Broadcast to every OTHER online member (not this instance, which you're already in)"
          }
        >
          {targetAgent ? (
            <>
              <span>@{targetAgent.name}</span>
              <span className="flr__target-x" aria-hidden>
                ×
              </span>
            </>
          ) : (
            <>
              <Radio size={13} />
              <span>Everyone else · {broadcastTargets.length}</span>
            </>
          )}
        </button>
        <input
          ref={inputRef}
          className="flr__input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={targetAgent ? `Message @${targetAgent.name}…` : "Message everyone…  (@ to address one)"}
          aria-label={targetAgent ? `Message ${targetAgent.name}` : "Broadcast message"}
        />
        <button
          type="button"
          className="flr__send"
          onClick={() => submit(false)}
          disabled={!draft.trim() || (!targetAgent && broadcastTargets.length === 0)}
          aria-label={targetAgent ? `Message ${targetAgent.name}` : "Broadcast"}
        >
          <Send size={15} />
        </button>
      </div>

      {diag && (
        <MemberDiagnostics
          key={diag.slug}
          member={{
            slug: diag.slug,
            name: diag.name,
            presenceKey: diagPresence.key,
            presenceLabel: diagPresence.label,
          }}
          onClose={() => setDiag(null)}
        />
      )}
    </div>
  );
}

// ── Member diagnostics drawer (#3169, consuming the #3168 contract) ───────────────────────
// Read-only, operator-authenticated, snapshot-refresh (NO live SSE following). Every failure
// mode resolves to an actionable inline state rather than a raw error or a blank panel.

/** A diagnostics read failure reduced to an actionable state. The proxy owns member liveness
 *  (ADR 0042 → 409 stopped / 502 unreachable / 504 timeout); the #3168 route owns its own
 *  local failures (401 unauthorized, 404 missing task, 503 no store). Anything else is the
 *  catch-all `error`, which shows the server detail verbatim. */
export type DiagFailureKind =
  | "stopped"
  | "unreachable"
  | "timeout"
  | "unauthorized"
  | "missing-task"
  | "disabled"
  | "error";

/** Map a thrown request error onto a failure kind. A thrown fetch with no HTTP response at
 *  all (the hub/member was never reached) reads as `unreachable` rather than a mystery. */
export function classifyDiagnosticsError(error: unknown): DiagFailureKind {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 401:
        return "unauthorized";
      case 404:
        return "missing-task";
      case 409:
        return "stopped";
      case 502:
        return "unreachable";
      case 503:
        return "disabled";
      case 504:
        return "timeout";
      default:
        return "error";
    }
  }
  return "unreachable";
}

/** Actionable copy for a failure kind, naming the inspected member. `detail` (the server
 *  message) is surfaced only for the catch-all `error` — the named kinds carry their own copy. */
export function diagnosticsFailureCopy(
  kind: DiagFailureKind,
  memberName: string,
  detail?: string,
): { title: string; hint: string } {
  switch (kind) {
    case "stopped":
      return { title: `${memberName} is stopped`, hint: "Start it from its roster row, then refresh." };
    case "unreachable":
      return {
        title: `Can't reach ${memberName}`,
        hint: "Its box may be offline or its URL wrong. Refresh to retry.",
      };
    case "timeout":
      return { title: `${memberName} timed out`, hint: "It's slow to respond right now — refresh in a moment." };
    case "unauthorized":
      return {
        title: `Not authorized for ${memberName}`,
        hint: "This member needs a credential the hub doesn't carry.",
      };
    case "missing-task":
      return { title: "No such task on this member", hint: "The id must be an EXACT match — check it and retry." };
    case "disabled":
      return { title: `Diagnostics unavailable on ${memberName}`, hint: "Its task store isn't configured." };
    default:
      return { title: `Couldn't read diagnostics from ${memberName}`, hint: detail || "Refresh to retry." };
  }
}

/** The presentational state of a logs snapshot — the deliberate-opt-out ("disabled") vs
 *  working-but-empty ("empty") distinction (#3168) is decided in one tested place. */
export type LogsView = "disabled" | "empty" | "lines";
export function logsView(logs: DiagnosticsLogs): LogsView {
  if (!logs.enabled) return "disabled";
  return logs.lines.length === 0 ? "empty" : "lines";
}

/** One async sub-read's render state. `null` (never given to the view) = not yet requested. */
export type DiagLoadState<T> =
  | { status: "loading" }
  | { status: "error"; kind: DiagFailureKind; detail?: string }
  | { status: "ready"; data: T };

function asLoadState<T>(q: UseQueryResult<T>): DiagLoadState<T> {
  if (q.isError) {
    return {
      status: "error",
      kind: classifyDiagnosticsError(q.error),
      detail: q.error instanceof Error ? q.error.message : undefined,
    };
  }
  if (q.data !== undefined) return { status: "ready", data: q.data };
  return { status: "loading" };
}

type DiagMember = { slug: string; name: string; presenceKey: PresenceKey; presenceLabel: string };

/** An inline failure card, shared by the logs + task sub-panels. */
function DiagFailure({ kind, detail, memberName }: { kind: DiagFailureKind; detail?: string; memberName: string }) {
  const { title, hint } = diagnosticsFailureCopy(kind, memberName, detail);
  return (
    <div className={`flr-diag__state flr-diag__state--${kind === "disabled" ? "muted" : "error"}`} role="status">
      <span className="flr-diag__state-title">{title}</span>
      <span className="flr-diag__state-hint">{hint}</span>
    </div>
  );
}

/** The bounded logs snapshot (or its disabled/empty state). */
function DiagLogsBody({ logs }: { logs: DiagnosticsLogs }) {
  const view = logsView(logs);
  if (view === "disabled") {
    return (
      <div className="flr-diag__state flr-diag__state--muted" role="status">
        <span className="flr-diag__state-title">Log buffer disabled</span>
        <span className="flr-diag__state-hint">{logs.note || "This member isn't retaining logs in memory."}</span>
      </div>
    );
  }
  if (view === "empty") {
    return (
      <div className="flr-diag__state flr-diag__state--muted" role="status">
        <span className="flr-diag__state-title">No log lines yet</span>
        <span className="flr-diag__state-hint">The ring is empty — refresh after the member does some work.</span>
      </div>
    );
  }
  return (
    <>
      {logs.note && <p className="flr-diag__note" role="status">{logs.note}</p>}
      <div className="flr-diag__meta">
        showing {logs.returned} of ≤{logs.capacity} retained · redacted at the member
      </div>
      <ol className="flr-diag__log">
        {logs.lines.map((ln, i) => (
          <li key={i} className="flr-diag__logline">
            {ln.ts && <span className="flr-diag__logts">{localStamp(ln.ts)}</span>}
            {ln.level && (
              <span className={`flr-diag__loglvl flr-diag__loglvl--${String(ln.level).toLowerCase()}`}>{ln.level}</span>
            )}
            <span className="flr-diag__logmsg">{ln.message}</span>
          </li>
        ))}
      </ol>
    </>
  );
}

/** The #3168 task summary — state, bounds/parse metadata, output, history + artifacts. */
function DiagTaskBody({ task }: { task: DiagnosticsTask }) {
  const flagged = task.truncated.length > 0 || task.malformed.length > 0;
  return (
    <div className="flr-diag__task">
      <div className="flr-diag__taskmeta">
        <span className="flr-diag__pill">{task.state || "unknown state"}</span>
        <code className="flr-diag__code">{task.task_id}</code>
        {task.last_updated && <span className="flr-diag__ago">{localStamp(task.last_updated)}</span>}
      </div>
      {flagged && (
        <div className="flr-diag__flags">
          {task.truncated.length > 0 && (
            <span className="flr-diag__flag flr-diag__flag--trunc" role="status">
              Truncated: {task.truncated.join(", ")}
            </span>
          )}
          {task.malformed.length > 0 && (
            <span className="flr-diag__flag flr-diag__flag--bad" role="status">
              Unparsed: {task.malformed.join(", ")}
            </span>
          )}
        </div>
      )}
      {task.status_message && <p className="flr-diag__statusmsg">{task.status_message}</p>}
      {task.accumulated_text && (
        <div className="flr-diag__field">
          <h4>Output</h4>
          <pre className="flr-diag__pre">{task.accumulated_text}</pre>
        </div>
      )}
      {task.history.length > 0 && (
        <div className="flr-diag__field">
          <h4>History ({task.history.length})</h4>
          <ul className="flr-diag__rows">
            {task.history.map((m, i) => (
              <li key={i} className="flr-diag__row">
                <span className="flr-diag__role">{m.role || "?"}</span>
                <span className="flr-diag__rowtext">{m.text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {task.artifacts.length > 0 && (
        <div className="flr-diag__field">
          <h4>Artifacts ({task.artifacts.length})</h4>
          <ul className="flr-diag__rows">
            {task.artifacts.map((a, i) => (
              <li key={i} className="flr-diag__row">
                <span className="flr-diag__role">{a.name || a.artifact_id || "artifact"}</span>
                <span className="flr-diag__rowtext">{a.text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** The drawer's presentation — a PURE function of its props (no hooks), so every state is
 *  render-testable in isolation. The stateful `MemberDiagnostics` owns the queries and feeds
 *  it. Always names the inspected member in the header (#3169 r3). */
export function DiagnosticsView(props: {
  member: DiagMember;
  logs: DiagLoadState<DiagnosticsLogs>;
  logsFetching: boolean;
  onRefreshLogs: () => void;
  onClose: () => void;
  taskIdDraft: string;
  onTaskIdChange: (value: string) => void;
  onInspectTask: () => void;
  /** null = the operator hasn't inspected a task yet (the idle prompt). */
  task: DiagLoadState<DiagnosticsTask> | null;
}) {
  const { member } = props;
  return (
    <div className="flr-diag" role="region" aria-label={`Diagnostics for ${member.name}`}>
      <div className="flr-diag__head">
        <button type="button" className="flr__icon" onClick={props.onClose} aria-label="Back to the roster">
          <ArrowLeft size={15} />
        </button>
        <span className={`flr__dot flr__dot--${member.presenceKey}`} aria-hidden />
        <div className="flr-diag__id">
          <span className="flr-diag__name">{member.name}</span>
          <span className="flr-diag__slug">
            {member.slug} · {member.presenceLabel}
          </span>
        </div>
        <span className="flr-diag__tag">read-only diagnostics</span>
      </div>

      <div className="flr-diag__body">
        <section className="flr-diag__section">
          <header className="flr-diag__sechead">
            <ScrollText size={13} aria-hidden />
            <h3>Recent logs</h3>
            <button
              type="button"
              className="flr__icon"
              onClick={props.onRefreshLogs}
              disabled={props.logsFetching}
              title="Refresh the log snapshot"
              aria-label="Refresh logs"
            >
              <RefreshCw size={13} className={props.logsFetching ? "flr-diag__spin" : undefined} />
            </button>
          </header>
          {props.logs.status === "loading" && (
            <div className="flr-diag__state" role="status">
              <span className="flr-diag__state-title">Loading logs…</span>
            </div>
          )}
          {props.logs.status === "error" && (
            <DiagFailure kind={props.logs.kind} detail={props.logs.detail} memberName={member.name} />
          )}
          {props.logs.status === "ready" && <DiagLogsBody logs={props.logs.data} />}
        </section>

        <section className="flr-diag__section">
          <header className="flr-diag__sechead">
            <Search size={13} aria-hidden />
            <h3>Inspect a task</h3>
          </header>
          <div className="flr-diag__taskform">
            <input
              className="flr-diag__taskinput"
              value={props.taskIdDraft}
              onChange={(e) => props.onTaskIdChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  props.onInspectTask();
                }
              }}
              placeholder="exact task id…"
              aria-label="Task id"
              spellCheck={false}
            />
            <button
              type="button"
              className="flr-diag__inspect"
              onClick={props.onInspectTask}
              disabled={!props.taskIdDraft.trim()}
            >
              Inspect
            </button>
          </div>
          {props.task === null && (
            <p className="flr-diag__idle">Enter an exact task id to see its bounded #3168 summary.</p>
          )}
          {props.task?.status === "loading" && (
            <div className="flr-diag__state" role="status">
              <span className="flr-diag__state-title">Loading task…</span>
            </div>
          )}
          {props.task?.status === "error" && (
            <DiagFailure kind={props.task.kind} detail={props.task.detail} memberName={member.name} />
          )}
          {props.task?.status === "ready" && <DiagTaskBody task={props.task.data} />}
        </section>
      </div>
    </div>
  );
}

/** The stateful drawer: owns the member-scoped, snapshot-refresh queries (NO polling, NO SSE)
 *  and drives `DiagnosticsView`. Mounted with `key={member.slug}` by the roster so switching to
 *  a different member starts a clean slate; a fleet poll never remounts it (the pinned slug is
 *  stable), so the inspected member never retargets. */
function MemberDiagnostics({ member, onClose }: { member: DiagMember; onClose: () => void }) {
  const logsQuery = useQuery({
    queryKey: ["memberDiagnostics", "logs", member.slug],
    queryFn: () => api.memberDiagnosticsLogs(member.slug),
    retry: false, // a 409/502/504/401 is a state to render, not a thing to spin on
    refetchOnWindowFocus: false,
    staleTime: Infinity, // snapshot refresh only — the Refresh button refetches on demand
    gcTime: 0,
  });

  const [taskIdDraft, setTaskIdDraft] = useState("");
  const [inspectedTaskId, setInspectedTaskId] = useState<string | null>(null);
  const taskQuery = useQuery({
    queryKey: ["memberDiagnostics", "task", member.slug, inspectedTaskId],
    queryFn: () => api.memberDiagnosticsTask(member.slug, inspectedTaskId as string),
    enabled: inspectedTaskId != null,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    gcTime: 0,
  });

  const inspect = () => {
    const id = taskIdDraft.trim();
    if (!id) return;
    if (id === inspectedTaskId) void taskQuery.refetch(); // re-inspect the same id ⇒ fresh read
    else setInspectedTaskId(id);
  };

  return (
    <DiagnosticsView
      member={member}
      logs={asLoadState(logsQuery)}
      logsFetching={logsQuery.isFetching}
      onRefreshLogs={() => void logsQuery.refetch()}
      onClose={onClose}
      taskIdDraft={taskIdDraft}
      onTaskIdChange={setTaskIdDraft}
      onInspectTask={inspect}
      task={inspectedTaskId == null ? null : asLoadState(taskQuery)}
    />
  );
}

/** The palette view (registered by usePaletteRegistry; entered by the "Fleet Room"
 *  command). Kept as a factory so the JSX lives here and usePaletteRegistry stays .ts.
 *  `onOpenAgent` routes through the registry's nav chokepoint so it also works forwarded
 *  from the frameless desktop launcher window (ADR 0057). */
export function fleetRoomView(opts: { onOpenAgent: (slug: string) => void }): PaletteView {
  return {
    id: "fleet-room",
    title: "Fleet",
    width: 780,
    footerHint: (
      <span className="flr__hint">
        <span>
          <kbd className="flr__kbd">click</kbd> DM a member
        </span>
        <span>
          <kbd className="flr__kbd">@</kbd> address in composer
        </span>
        <span>
          <kbd className="flr__kbd">↵</kbd> send · <kbd className="flr__kbd">⌘↵</kbd> broadcast
        </span>
      </span>
    ),
    render: (ctx) => <FleetRoom ctx={ctx} onOpenAgent={opts.onOpenAgent} />,
  };
}
