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
import { Activity, AlertTriangle, ChevronLeft, ExternalLink, FileText, Play, RefreshCw, Radio, ScrollText, Send, Square } from "lucide-react";
import { useToast } from "@protolabsai/ui/overlays";
import type { PaletteContext, PaletteView } from "@protolabsai/ui/command-palette";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, currentSlug } from "../lib/api";
import { fleetQuery, queryKeys } from "../lib/queries";
import { errMsg } from "../lib/format";
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
  // The member the diagnostics drawer is inspecting — the operator's EXPLICIT pick (ADR 0042
  // §authoritative-drawer-state, #3169). Held here, independent of the composer `target` and
  // the focused-window slug, so a fleet-selection change never retargets an open drawer; only
  // a fresh Diagnostics click does. Null = the drawer is closed (the activity feed shows).
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

  // Inspect a member's diagnostics IN the room (no navigation, ADR 0042). Available for every
  // member — including a stopped/unreachable one, which is exactly when an operator reaches for
  // it (the drawer then renders the actionable failure state). Captures the id+name at click
  // time so the drawer stays bound to THIS member even as the roster re-polls or a fleet
  // selection changes; `slug` is the immutable id, never the editable display name.
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
                <div
                  key={slug}
                  className={`flr__member${a.running ? "" : " is-down"}${diag?.slug === slug ? " is-diag" : ""}`}
                >
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
                      className={`flr__icon${diag?.slug === slug ? " is-active" : ""}`}
                      onClick={() => openDiag(a)}
                      title="Diagnostics — bounded logs & task inspector"
                      aria-label={`Diagnostics for ${a.name}`}
                      aria-pressed={diag?.slug === slug}
                    >
                      <Activity size={14} />
                    </button>
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
          {diag ? (
            <MemberDiagnostics
              key={diag.slug}
              slug={diag.slug}
              name={diag.name}
              agent={roster.find((a) => slugOf(a) === diag.slug)}
              onClose={() => setDiag(null)}
            />
          ) : (
            <FleetActivityFeed />
          )}
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
    </div>
  );
}

/** An inspected member's diagnostics failure, mapped to an actionable inline state (#3169).
 *
 *  Failure containment is server-owned: a member that's stopped / unreachable / slow is the
 *  hub proxy's case (409 / 502 / 504 — never a 500, ADR 0042), and the member's OWN local
 *  failure modes (a missing task, no task store) return a structured non-500 with their own
 *  status. Both surface on the thrown `ApiError.status`, so this maps each to a titled,
 *  next-step hint rather than a raw error string. `subject` disambiguates the two reads (a 404
 *  from the task inspector is a missing task; from logs it's a plain not-found). */
export type DiagErrorKind =
  | "stopped"
  | "unreachable"
  | "timeout"
  | "unauthorized"
  | "unconfigured"
  | "missing"
  | "error";
export function diagnosticErrorState(
  error: unknown,
  subject: "logs" | "task",
): { kind: DiagErrorKind; title: string; hint: string } {
  const status = error instanceof ApiError ? error.status : 0;
  switch (status) {
    case 401:
      return {
        kind: "unauthorized",
        title: "Not authorized",
        hint: "This member needs a valid operator token before its diagnostics can be read.",
      };
    case 409:
      return {
        kind: "stopped",
        title: "Member is stopped",
        hint: "The agent isn’t running, so it can’t answer. Start it from the roster, then retry.",
      };
    case 502:
      return {
        kind: "unreachable",
        title: "Member unreachable",
        hint: "The hub couldn’t reach this member — a remote box offline, or its URL is wrong.",
      };
    case 504:
      return {
        kind: "timeout",
        title: "Timed out",
        hint: "The member didn’t respond in time. Retry, or check whether it’s overloaded.",
      };
    case 503:
      return {
        kind: "unconfigured",
        title: "No task store",
        hint: "This member has no A2A task store configured, so its tasks can’t be inspected.",
      };
    case 404:
      return subject === "task"
        ? {
            kind: "missing",
            title: "No such task",
            hint: "No task with that id exists on this member. Check the id and try again.",
          }
        : { kind: "error", title: "Not found", hint: errMsg(error) };
    default:
      return {
        kind: "error",
        title: subject === "task" ? "Couldn’t inspect the task" : "Couldn’t load logs",
        hint: errMsg(error),
      };
  }
}

function DiagError({
  state,
  onRetry,
}: {
  state: { kind: DiagErrorKind; title: string; hint: string };
  onRetry: () => void;
}) {
  return (
    <div className={`flr__diag-error flr__diag-error--${state.kind}`} role="alert" data-testid={`diag-error-${state.kind}`}>
      <AlertTriangle size={15} className="flr__diag-erricon" aria-hidden />
      <div className="flr__diag-errbody">
        <strong>{state.title}</strong>
        <span>{state.hint}</span>
      </div>
      <button type="button" className="flr__diag-retry" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** The member diagnostics drawer (#3169) — bounded logs + exact A2A task inspection for the
 *  member the operator explicitly picked, consuming ONLY #3168's operator-authenticated,
 *  slug-scoped proxy reads. Snapshot refresh only (a Refresh button; no live SSE following).
 *  Bounds + redaction are server-owned — this only presents the returned snapshot and its
 *  `note` / `truncated` / `malformed` metadata. `agent` is the LIVE roster row (for presence);
 *  it may be undefined if the member has since left the fleet, in which case the drawer still
 *  identifies it by the captured `name` and lets its reads fail into an actionable state. */
export function MemberDiagnostics({
  slug,
  name,
  agent,
  onClose,
}: {
  slug: string;
  name: string;
  agent?: FleetAgent;
  onClose: () => void;
}) {
  const [taskDraft, setTaskDraft] = useState("");
  const [taskId, setTaskId] = useState<string | null>(null);
  const presence = agent ? presenceOf(agent) : { key: "unreachable" as const, label: "left the fleet" };

  const logs = useQuery({
    queryKey: ["diagnostics", "logs", slug],
    queryFn: () => api.memberDiagnosticsLogs(slug),
    // A 409/502/504/401 is an actionable state, not a transient to ride out — don't let the
    // shared cold-start retry mask it behind a spinner. Snapshot read: the only re-fetch is
    // the Refresh button; there is deliberately no poll and no SSE follow.
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const task = useQuery({
    queryKey: ["diagnostics", "task", slug, taskId],
    queryFn: () => api.memberDiagnosticsTask(slug, taskId as string),
    enabled: taskId !== null,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const inspect = () => {
    const id = taskDraft.trim();
    if (id) setTaskId(id);
  };
  const onTaskKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      inspect();
    }
  };

  return (
    <div className="flr__diag" data-testid="fleet-diagnostics">
      <div className="flr__diag-head">
        <button
          type="button"
          className="flr__diag-back"
          onClick={onClose}
          title="Back to activity"
          aria-label="Close diagnostics"
        >
          <ChevronLeft size={16} />
        </button>
        <span className={`flr__dot flr__dot--${presence.key}`} aria-hidden />
        <span className="flr__diag-name" data-testid="diag-member">
          {name}
        </span>
        <span className="flr__diag-sub">diagnostics · {presence.label}</span>
      </div>

      <div className="flr__diag-body">
        <section className="flr__diag-section">
          <header className="flr__diag-sechead">
            <span className="flr__diag-sectitle">
              <ScrollText size={13} aria-hidden /> Logs
            </span>
            <button
              type="button"
              className="flr__diag-refresh"
              onClick={() => logs.refetch()}
              disabled={logs.isFetching}
              aria-label="Refresh logs"
              title="Refresh logs (snapshot)"
            >
              <RefreshCw size={13} aria-hidden /> Refresh
            </button>
          </header>
          {logs.isError ? (
            <DiagError state={diagnosticErrorState(logs.error, "logs")} onRetry={() => logs.refetch()} />
          ) : logs.data ? (
            <LogsView data={logs.data} />
          ) : (
            <div className="flr__diag-state">Loading logs…</div>
          )}
        </section>

        <section className="flr__diag-section">
          <header className="flr__diag-sechead">
            <span className="flr__diag-sectitle">
              <FileText size={13} aria-hidden /> Task inspector
            </span>
          </header>
          <div className="flr__diag-taskbar">
            <input
              className="flr__diag-taskinput"
              value={taskDraft}
              onChange={(e) => setTaskDraft(e.target.value)}
              onKeyDown={onTaskKeyDown}
              placeholder="Exact task id…"
              aria-label="Task id"
              spellCheck={false}
            />
            <button
              type="button"
              className="flr__diag-inspect"
              onClick={inspect}
              disabled={!taskDraft.trim()}
            >
              Inspect
            </button>
          </div>
          {taskId === null ? (
            <div className="flr__diag-state flr__diag-idle">
              Enter an exact task id to inspect a turn on {name}.
            </div>
          ) : task.isError ? (
            <DiagError state={diagnosticErrorState(task.error, "task")} onRetry={() => task.refetch()} />
          ) : task.data ? (
            <TaskView t={task.data} />
          ) : (
            <div className="flr__diag-state">Inspecting {taskId}…</div>
          )}
        </section>
      </div>
    </div>
  );
}

/** Render a bounded log snapshot. `enabled:false` is a first-class state (an unconfigured or
 *  opted-out buffer — `note` says which), distinct from an enabled-but-empty buffer; neither
 *  is an error. A clamped `lines=` request also arrives on `note`. */
function LogsView({ data }: { data: DiagnosticsLogs }) {
  return (
    <>
      {data.note && (
        <div className="flr__diag-note" role="note">
          {data.note}
        </div>
      )}
      {!data.enabled ? (
        <div className="flr__diag-state" data-testid="diag-logs-disabled">
          Log buffer unavailable — this member retains nothing in memory to show.
        </div>
      ) : data.returned === 0 ? (
        <div className="flr__diag-state" data-testid="diag-logs-empty">
          No log lines retained yet.
        </div>
      ) : (
        <>
          <div className="flr__diag-meta">
            snapshot · {data.returned} of up to {data.capacity} retained lines
          </div>
          <ol className="flr__diag-logs" data-testid="diag-logs">
            {data.lines.map((line, i) => (
              <li key={i} className="flr__diag-logline">
                {line}
              </li>
            ))}
          </ol>
        </>
      )}
    </>
  );
}

/** Render one inspected task's #3168 summary. `truncated` / `malformed` are surfaced as an
 *  explicit banner — a silently trimmed history reads as "that's everything", and a swallowed
 *  parse failure lets an operator draw a conclusion from a partial answer without knowing it. */
function TaskView({ t }: { t: DiagnosticsTask }) {
  const bounded = t.truncated.length > 0 || t.malformed.length > 0;
  return (
    <div className="flr__diag-task" data-testid="diag-task">
      <div className="flr__diag-taskhead">
        <span className="flr__diag-pill">{t.state ?? "unknown state"}</span>
        {t.last_updated && <span className="flr__diag-taskmeta">updated {t.last_updated}</span>}
      </div>
      {t.context_id && <div className="flr__diag-taskmeta">context {t.context_id}</div>}
      {bounded && (
        <div className="flr__diag-note flr__diag-note--warn" role="note" data-testid="diag-task-notes">
          {t.truncated.length > 0 && <div>Truncated (server-bounded): {t.truncated.join(", ")}.</div>}
          {t.malformed.length > 0 && (
            <div>Malformed — couldn’t parse: {t.malformed.join(", ")}. Shown partially.</div>
          )}
        </div>
      )}
      {t.status_message && (
        <div className="flr__diag-block">
          <span className="flr__diag-blocklabel">status message</span>
          <div className="flr__diag-blocktext">{t.status_message}</div>
        </div>
      )}
      {t.accumulated_text && (
        <div className="flr__diag-block">
          <span className="flr__diag-blocklabel">accumulated output</span>
          <pre className="flr__diag-pre">{t.accumulated_text}</pre>
        </div>
      )}
      {t.history.length > 0 && (
        <div className="flr__diag-block">
          <span className="flr__diag-blocklabel">history · {t.history.length}</span>
          <ol className="flr__diag-msgs">
            {t.history.map((m, i) => (
              <li key={m.message_id ?? i} className="flr__diag-msg">
                <span className="flr__diag-role">{m.role ?? "?"}</span>
                <span className="flr__diag-msgtext">{m.text || "—"}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
      {t.artifacts.length > 0 && (
        <div className="flr__diag-block">
          <span className="flr__diag-blocklabel">artifacts · {t.artifacts.length}</span>
          <ol className="flr__diag-msgs">
            {t.artifacts.map((a, i) => (
              <li key={a.artifact_id ?? i} className="flr__diag-msg">
                <span className="flr__diag-role">{a.name ?? "artifact"}</span>
                <span className="flr__diag-msgtext">{a.text || "—"}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
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
