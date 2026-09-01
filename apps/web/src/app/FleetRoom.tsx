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
import type { FormEvent as ReactFormEvent, KeyboardEvent as ReactKeyboardEvent } from "react";
import { ExternalLink, Play, Radio, RefreshCw, Send, Square, Stethoscope, X } from "lucide-react";
import { useToast } from "@protolabsai/ui/overlays";
import type { PaletteContext, PaletteView } from "@protolabsai/ui/command-palette";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  api,
  currentSlug,
  is401,
  isAgentNotRunning,
  isAgentTimeout,
  isAgentUnreachable,
} from "../lib/api";
import { fleetQuery, queryKeys } from "../lib/queries";
import { errMsg } from "../lib/format";
import type { FleetAgent, MemberDiagnosticsTask } from "../lib/types";
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
  // The member whose diagnostics drawer is open — DRAWER-LOCAL and authoritative (ADR 0042):
  // a snapshot of the explicitly selected member. It is NEVER derived from `target` (the
  // composer address) or `currentSlug()` (the window's focused agent), so the drawer does not
  // retarget when the ambient fleet selection changes; only clicking Diagnostics moves it.
  const [diag, setDiag] = useState<FleetAgent | null>(null);
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
              const isDiag = diag ? slugOf(diag) === slug : false;
              return (
                <div
                  key={slug}
                  className={`flr__member${a.running ? "" : " is-down"}${isDiag ? " is-diag" : ""}`}
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
                      className="flr__icon"
                      onClick={() => setDiag(a)}
                      title="Diagnostics — bounded logs + task inspection"
                      aria-label={`Diagnostics for ${a.name}`}
                    >
                      <Stethoscope size={14} />
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
        // Overlaid, not swapped in place of a column, so the activity feed stays MOUNTED
        // underneath (its SSE keeps feeding the presence pills). Re-resolve the live member by
        // its frozen slug so presence stays fresh, but fall back to the drawer-local snapshot if
        // it leaves the fleet — the drawer keeps identifying the member it was opened for. Keyed
        // on slug so an explicit switch to a different member gets a clean drawer.
        <MemberDiagnosticsDrawer
          key={slugOf(diag)}
          slug={slugOf(diag)}
          member={roster.find((a) => slugOf(a) === slugOf(diag)) ?? diag}
          onClose={() => setDiag(null)}
        />
      )}
    </div>
  );
}

/** An actionable inline state for a drawer read that couldn't complete (#3169). Maps the
 *  proxy/API status onto operator-facing copy: the proxy answers 409 (stopped) / 502
 *  (unreachable) / 504 (timeout); the member answers 401 (unauthorized), and — for a task —
 *  404 (missing) / 503 (no task store). Anything else falls through to the raw detail. */
type DiagErrorView = { title: string; hint: string };
function describeDiagError(
  error: unknown,
  name: string,
  kind: "logs" | "task",
  taskId?: string,
): DiagErrorView {
  if (is401(error)) {
    return {
      title: "Operator sign-in required",
      hint: `${name} rejected the operator credential (401). Re-authenticate to read its diagnostics.`,
    };
  }
  if (isAgentNotRunning(error)) {
    return {
      title: `${name} is stopped`,
      hint:
        kind === "logs"
          ? "Start it from the roster to read its logs."
          : "Start it from the roster to inspect its tasks.",
    };
  }
  if (isAgentUnreachable(error)) {
    return {
      title: `${name} is unreachable`,
      hint: "The hub can't reach this member — check that its box is online and its URL is right.",
    };
  }
  if (isAgentTimeout(error)) {
    return { title: `${name} timed out`, hint: "It didn't answer in time. Retry, or check whether it's overloaded." };
  }
  if (kind === "task" && error instanceof ApiError && error.status === 404) {
    return { title: "No such task", hint: `No task “${taskId ?? ""}” on ${name}.` };
  }
  if (kind === "task" && error instanceof ApiError && error.status === 503) {
    return {
      title: "Task store unavailable",
      hint: `${name} has no task store configured, so its tasks can't be inspected.`,
    };
  }
  return { title: "Couldn't load diagnostics", hint: errMsg(error) };
}

function DiagErrorState({ view, onRetry }: { view: DiagErrorView; onRetry: () => void }) {
  return (
    <div className="flr__diag-state flr__diag-state--error" role="alert">
      <strong>{view.title}</strong>
      <span>{view.hint}</span>
      <button type="button" className="flr__diag-retry" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** The diagnostics drawer (#3169), bound to ONE explicitly selected member. It fetches the
 *  #3168 operator-authenticated, slug-scoped proxy endpoints for that member — never the
 *  window's focused agent — and renders bounded snapshots plus actionable states. Snapshot
 *  refresh only (a manual Refresh button); live SSE log following is deliberately excluded.
 *  Exported for component tests. */
export function MemberDiagnosticsDrawer({
  slug,
  member,
  onClose,
}: {
  slug: string;
  member: FleetAgent;
  onClose: () => void;
}) {
  const name = member.name;
  const p = presenceOf(member);
  const [taskField, setTaskField] = useState("");
  // The id actually inspected — set on submit, so retyping doesn't refetch until Inspect.
  const [taskId, setTaskId] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => closeRef.current?.focus(), []);

  const logs = useQuery({
    queryKey: ["fleet-diagnostics-logs", slug],
    queryFn: () => api.memberDiagnosticsLogs(slug),
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  const task = useQuery({
    queryKey: ["fleet-diagnostics-task", slug, taskId],
    queryFn: () => api.memberDiagnosticsTask(slug, taskId),
    enabled: taskId !== "",
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });

  const inspect = (e: ReactFormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const id = taskField.trim();
    if (id) setTaskId(id);
  };

  return (
    <div
      className="flr__diag"
      role="region"
      aria-label={`Diagnostics for ${name}`}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="flr__diag-head">
        <div className="flr__diag-id">
          <span className={`flr__dot flr__dot--${p.key}`} aria-hidden />
          <div>
            <div className="flr__diag-name">
              {name}
              <span className="flr__tag">diagnostics</span>
            </div>
            <div className="flr__diag-slug">
              {slug} · {p.label}
            </div>
          </div>
        </div>
        <div className="flr__diag-headactions">
          <button
            type="button"
            className="flr__icon"
            onClick={() => logs.refetch()}
            title="Refresh the log snapshot"
            aria-label={`Refresh ${name} logs`}
            disabled={logs.isFetching}
          >
            <RefreshCw size={14} />
          </button>
          <button
            ref={closeRef}
            type="button"
            className="flr__icon"
            onClick={onClose}
            title="Close diagnostics"
            aria-label="Close diagnostics"
          >
            <X size={15} />
          </button>
        </div>
      </div>

      <div className="flr__diag-body">
        <section className="flr__diag-section">
          <div className="flr__diag-sechead">
            <h3>Logs</h3>
            {logs.data?.enabled && (
              <span className="flr__diag-cap">
                {logs.data.returned} of {logs.data.capacity}
              </span>
            )}
          </div>
          {logs.isLoading ? (
            <div className="flr__diag-state">Loading {name}&rsquo;s logs&hellip;</div>
          ) : logs.isError ? (
            <DiagErrorState view={describeDiagError(logs.error, name, "logs")} onRetry={() => logs.refetch()} />
          ) : logs.data && !logs.data.enabled ? (
            <div className="flr__diag-state">
              <strong>Log buffer off</strong>
              <span>{logs.data.note ?? "This member is not retaining logs in memory."}</span>
            </div>
          ) : logs.data && logs.data.returned === 0 ? (
            <div className="flr__diag-state">
              <strong>No log lines yet</strong>
              <span>The buffer is enabled but empty. Refresh after the member does some work.</span>
            </div>
          ) : logs.data ? (
            <>
              {logs.data.note && <div className="flr__diag-note">{logs.data.note}</div>}
              <ol className="flr__diag-logs">
                {logs.data.lines.map((line, i) => (
                  <li className="flr__diag-log" key={`${line.ts}-${i}`}>
                    <span className="flr__diag-log-ts">{line.ts}</span>
                    <span className={`flr__diag-log-lvl flr__diag-log-lvl--${line.level.toLowerCase()}`}>
                      {line.level}
                    </span>
                    <span className="flr__diag-log-msg">
                      <span className="flr__diag-log-logger">{line.logger}</span> {line.message}
                    </span>
                  </li>
                ))}
              </ol>
            </>
          ) : null}
        </section>

        <section className="flr__diag-section">
          <div className="flr__diag-sechead">
            <h3>Inspect task</h3>
          </div>
          <form className="flr__diag-taskform" onSubmit={inspect}>
            <input
              className="flr__diag-taskinput"
              value={taskField}
              onChange={(e) => setTaskField(e.target.value)}
              placeholder="Exact A2A task id"
              aria-label={`Task id for ${name}`}
              spellCheck={false}
              autoComplete="off"
            />
            <button type="submit" className="flr__diag-inspect" disabled={!taskField.trim()}>
              Inspect
            </button>
          </form>
          {taskId === "" ? (
            <div className="flr__diag-state">
              Enter an exact task id to see its #3168 summary for {name} — state, output, history and
              artifacts.
            </div>
          ) : task.isLoading ? (
            <div className="flr__diag-state">Inspecting task {taskId}&hellip;</div>
          ) : task.isError ? (
            <DiagErrorState
              view={describeDiagError(task.error, name, "task", taskId)}
              onRetry={() => task.refetch()}
            />
          ) : task.data ? (
            <DiagTaskView task={task.data} />
          ) : null}
        </section>
      </div>
    </div>
  );
}

/** The #3168 task summary — bounded output + history/artifacts, with truncation/malformed
 *  metadata surfaced as badges so a partial view can't read as complete. */
function DiagTaskView({ task }: { task: MemberDiagnosticsTask }) {
  return (
    <div className="flr__diag-task">
      <div className="flr__diag-taskmeta">
        <span>
          <b>state</b> {task.state ?? "unknown"}
        </span>
        {task.last_updated && (
          <span>
            <b>updated</b> {task.last_updated}
          </span>
        )}
        {task.task_id && (
          <span>
            <b>id</b> {task.task_id}
          </span>
        )}
      </div>

      {(task.truncated.length > 0 || task.malformed.length > 0) && (
        <div className="flr__diag-badges">
          {task.truncated.map((field) => (
            <span className="flr__diag-badge" key={`t-${field}`} title="The reader capped this field">
              truncated: {field}
            </span>
          ))}
          {task.malformed.map((field) => (
            <span className="flr__diag-badge flr__diag-badge--malformed" key={`m-${field}`} title="Unparseable column">
              malformed: {field}
            </span>
          ))}
        </div>
      )}

      {task.status_message && (
        <div>
          <div className="flr__diag-subhead">Status message</div>
          <pre className="flr__diag-pre">{task.status_message}</pre>
        </div>
      )}

      {task.accumulated_text && (
        <div>
          <div className="flr__diag-subhead">Output</div>
          <pre className="flr__diag-pre">{task.accumulated_text}</pre>
        </div>
      )}

      {task.history.length > 0 && (
        <div>
          <div className="flr__diag-subhead">History ({task.history.length})</div>
          <div className="flr__diag-turns">
            {task.history.map((msg, i) => (
              <div className="flr__diag-turn" key={msg.message_id ?? `h-${i}`}>
                <span className="flr__diag-turnrole">{msg.role ?? "?"}</span>
                <span className="flr__diag-turntext">{msg.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {task.artifacts.length > 0 && (
        <div>
          <div className="flr__diag-subhead">Artifacts ({task.artifacts.length})</div>
          <div className="flr__diag-turns">
            {task.artifacts.map((art, i) => (
              <div className="flr__diag-turn" key={art.artifact_id ?? `a-${i}`}>
                <span className="flr__diag-turnrole">{art.name ?? "artifact"}</span>
                <span className="flr__diag-turntext">{art.text}</span>
              </div>
            ))}
          </div>
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
