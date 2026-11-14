import { useWorkActionSize } from "../../shell/SurfaceFrame";
/**
 * Work view ▸ Tasks section (#4955 §2.7).
 *
 * Registers itself on import. One component renders both panes of the Work view: the list
 * of `WorkRow`s (when no item is open) and the `WorkDetail` for the open item — told apart
 * by `state.itemId`, the way the shell drives them (WorkView §3.1).
 *
 * Tasks fold into jobs (work-consolidation §1): every row's tag is "Job", never "Task". A
 * not-started job (open/deferred, `groupFor` → Coming up) gets a dashed-circle icon, the meta
 * "not started · added <when>", and Start now / Close job; a started one keeps the base
 * board moves. Closed jobs leave the list — finished work leaves Work.
 *
 * The detail shows §2.7's fields: type and priority as "Bug · high" (numeric priority 0–3
 * reads urgent / high / normal / low), assignee, updated/closed times, a clamped
 * description and the created date. The only "Never shown" field, the id, lives in
 * Technical details. Actions go through the base `TasksPanel` board controls: the Start now /
 * Start / Mark open and Close job / Close / Reopen status transitions (the PATCH/close routes),
 * Edit (the existing PATCH, reusing `TaskCreateDialog`) and Delete…, which hands back an
 * `undo_token` (bd-0ia51.5) driving the §4.5 undo toast.
 */
import { useRef, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { FormField, Input } from "@protolabsai/ui/forms";
import { CircleDashed } from "lucide-react";
import { WorkEmpty as Empty } from "../WorkEmpty";
import { useToast } from "@protolabsai/ui/overlays";

import { chatStore } from "../../chat/chat-store";
import { api } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import type { Task } from "../../lib/types";
import { TaskCreateDialog } from "../../app/TasksPanel";
import { issueStatus, issueType, type IssueDraft } from "../../app/tasks";
import { groupFor, relTime, staleTaskMeta, taskStaleDays, type GroupableWorkItem } from "../tone";
import { typeAndPriority } from "../rowMeta";
import { WorkDetail, type WorkAction, type WorkField } from "../WorkDetail";
import { WorkRow } from "../WorkRow";
import { registerWorkSection, type WorkSectionContext } from "../sectionRegistry";
import type { WorkItem, WorkSource } from "../useWorkItems";

const CLAMP_STYLE: CSSProperties = {
  display: "-webkit-box",
  WebkitLineClamp: 8,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
  whiteSpace: "pre-wrap",
};

/** Description, clamped to 8 lines with a Show full text toggle (§2.7 field 7). */
function Description({ text }: { text: string }) {
  const size = useWorkActionSize();
  const [open, setOpen] = useState(false);
  const long = text.split("\n").length > 8 || text.length > 400;
  if (!long) return <div className="work-task-desc" style={{ whiteSpace: "pre-wrap" }}>{text}</div>;
  return (
    <div>
      <div className="work-task-desc" style={open ? { whiteSpace: "pre-wrap" } : CLAMP_STYLE}>{text}</div>
      <Button variant="ghost" size={size} onClick={() => setOpen(!open)}>
        {open ? "Show less" : "Show full text"}
      </Button>
    </div>
  );
}

function taskFields(task: Task): WorkField[] {
  const fields: WorkField[] = [{ label: "Type and priority", value: typeAndPriority(task) }];
  if (task.assignee) fields.push({ label: "Assignee", value: `Assigned to ${task.assignee}` });
  // The 24h quiet line (ADR 0129) — muted information in a plain field, never a warning.
  const staleDays = taskStaleDays(task);
  if (staleDays != null) fields.push({ label: "Activity", value: staleTaskMeta(staleDays) });
  if (task.updated_at) fields.push({ label: "Updated", value: `Updated ${relTime(task.updated_at).text}` });
  if (task.closed_at) fields.push({ label: "Closed", value: `Closed ${relTime(task.closed_at).text}` });
  if (task.description?.trim()) {
    fields.push({ label: "Description", value: <Description text={task.description} /> });
  }
  if (task.created_at) fields.push({ label: "Created", value: `Created ${relTime(task.created_at).text}` });
  return fields;
}

function taskDraft(task: Task): IssueDraft {
  const priority = Number(task.priority);
  return {
    title: task.title,
    description: task.description ?? "",
    type: issueType(task),
    priority: Number.isFinite(priority) ? priority : 2,
  };
}

// Tasks fold into jobs (work-consolidation §1, ADR Q1): there is no Task tag. A job that
// hasn't started — an open or deferred former task, which `groupFor` places in Coming up —
// reads as not-started. A started one lands in Doing now: in_progress, or blocked, which
// `groupFor` counts as started work (the task API has no start history, and a task only
// reaches blocked by being acted on). Mirroring the shared selector keeps a row in the
// same group wherever it shows.
function jobNotStarted(item: WorkItem): boolean {
  return groupFor(item as GroupableWorkItem) === "coming-up";
}

// Finished jobs leave Work (§1): a closed former task falls out of every group
// (`groupFor` → null) — the same rule the shared `workGroups` counts apply.
function jobShowsInWork(item: WorkItem): boolean {
  return groupFor(item as GroupableWorkItem) !== null;
}

// Row meta (§2): a not-started job says "not started · added <when>"; a blocked (started)
// job says "blocked"; one parked on the operator (`waiting`, ADR 0129 Q1) says so; an
// `in_progress` one quiet for a day leads with the muted stale line (ADR 0129 — the work
// may have finished elsewhere without a task_close); any other started job keeps its type
// and priority.
function jobMeta(item: WorkItem): string {
  const task = item.raw as Task;
  if (jobNotStarted(item)) {
    const added = task.created_at ? relTime(task.created_at).text : "";
    return added ? `not started · added ${added}` : "not started";
  }
  if (issueStatus(task) === "blocked") return "blocked";
  if (issueStatus(task) === "waiting") return "waiting on you";
  const staleDays = taskStaleDays(task);
  if (staleDays != null) return `${staleTaskMeta(staleDays)} · ${typeAndPriority(task)}`;
  return typeAndPriority(task);
}

async function sourceHasTurns(sessionId: string): Promise<boolean> {
  if (!sessionId) return false;
  const result = await api.chatSessionTurns(sessionId, 1);
  // This read endpoint also reports store outages as a successful, empty response.
  if (result.reason) throw new Error(result.reason);
  return result.turns.length > 0;
}

/** Waiting jobs are answered here even when their originating session was never a chat. */
function WaitingTaskDetail({ task, actions, openChat, refresh }: {
  task: Task; actions: WorkAction[]; openChat?: (sessionId: string) => void | Promise<void>;
  refresh: () => Promise<unknown>;
}) {
  const size = useWorkActionSize();
  const [reply, setReply] = useState("");
  const [closing, setClosing] = useState(false);
  // Keep the destination and message id through a failed PATCH or a lost send response.
  // Retrying finishes the same reply instead of creating another chat or turn.
  const attempt = useRef<{ sessionId: string; id: string; text: string; sent: boolean; fresh: boolean } | null>(null);
  const source = task.session_id ?? "";
  const history = useQuery({
    queryKey: ["work-task-source", task.id, source],
    queryFn: () => sourceHasTurns(source),
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnMount: "always",
  });
  const ask = task.ask?.trim() || task.description?.split("\n")[0]?.trim() || "";
  const send = useMutation({
    mutationFn: async () => {
      if (!attempt.current) {
        // Recheck before sending: a source may have gained turns since detail opened.
        const hasTurns = await sourceHasTurns(source);
        attempt.current = {
          sessionId: hasTurns ? source : `chat-${crypto.randomUUID()}`,
          id: crypto.randomUUID(), fresh: !hasTurns, sent: false,
          text: hasTurns ? reply.trim() : [task.title, ask].filter(Boolean).map((text) =>
            text.split("\n").map((line) => `> ${line}`).join("\n")).join("\n>\n") + `\n\n${reply.trim()}`,
        };
      }
      const pending = attempt.current;
      if (pending.fresh) await api.registerSession(pending.sessionId, { title: `Re: ${task.title}` });
      if (!pending.sent) {
        const result = await api.steerChat(pending.sessionId, pending.id, pending.text);
        if (!result.ok || (pending.fresh && !result.started_turn)) throw new Error("Reply has not started yet");
        pending.sent = true;
      }
      await api.updateTask(task.id, { session_id: pending.sessionId, status: "open" });
      await refresh();
      await openChat?.(pending.sessionId);
      if (pending.fresh) chatStore.renameSession(pending.sessionId, `Re: ${task.title}`);
    },
  });
  const waitingActions: WorkAction[] = (["done", "dismissed"] as const).map((outcome) => ({
    id: outcome, label: outcome === "done" ? "Done…" : "Dismiss…",
    destructive: outcome === "dismissed",
    confirm: {
      prompt: outcome === "done" ? "Mark this job done?" : "Dismiss this job?",
      explanation: "The agent will be told the outcome and your reason.",
      confirmLabel: outcome === "done" ? "Done" : "Dismiss", keepLabel: "Keep it",
      reason: { label: "Reason (optional)" },
    },
    run: async (reason) => {
      setClosing(true);
      try { await api.closeTask(task.id, reason, outcome); await refresh(); }
      finally { setClosing(false); }
    },
  }));
  if (history.data && !history.isError && openChat) waitingActions.push({
    id: "open-chat", label: "Open chat", run: async () => openChat(source),
  });
  const provenance = source.startsWith("job:") ? "Started by a background job"
    : source.startsWith("chat-") ? "Its chat was deleted" : "Started in an agent-to-agent conversation";
  return <WorkDetail title={task.title} status="needs-you" type="Job" technical={{ Id: task.id }}
    onUndo={async (token) => { await api.undo(token); await refresh(); }}
    actions={send.isPending ? [] : [...waitingActions, ...actions]}
    fields={[
      { label: "Ask", value: ask },
      { label: "Links", value: task.links?.length ? <ul>{task.links.map(({ ref }) => <li key={ref}>
        {/^(https?:)\/\//i.test(ref) ? <a href={ref} target="_blank" rel="noreferrer">{ref}</a> : ref}
      </li>)}</ul> : null },
      { label: "Waiting since", value: task.updated_at || task.created_at
        ? relTime((task.updated_at || task.created_at)!).text : null },
      { label: "Description", value: task.description ? <Description text={task.description} /> : null },
      ...taskFields(task).filter((field) => field.label !== "Description"),
      { label: "Source", value: history.isError ? <Alert status="warning">Couldn't check this chat. <Button size={size}
        onClick={() => void history.refetch()}>Retry</Button></Alert>
        : history.isPending ? "Checking source chat…" : !history.data ? provenance : null },
      { label: "Reply", value: <form onSubmit={(event) => { event.preventDefault(); if (reply.trim() && !send.isPending && !closing && !history.isPending && !history.isError) send.mutate(); }}>
        <FormField label="Your reply"><Input value={reply} disabled={send.isPending || !!attempt.current}
          onChange={(event) => setReply(event.target.value)} /></FormField>
        <Button size={size} type="submit" disabled={!reply.trim() || history.isPending || history.isError || send.isPending || closing}>
          {send.isError ? "Retry reply" : "Send reply"}
        </Button>
        {send.isError && <Alert status="warning">Couldn't finish sending your reply. Retry to continue.</Alert>}
      </form> },
    ]} />;
}

function TaskDetail({ item, openChat }: { item: WorkItem; openChat?: (sessionId: string) => void | Promise<void> }) {
  const task = item.raw as Task;
  const qc = useQueryClient();
  const toast = useToast();
  const [editOpen, setEditOpen] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: queryKeys.tasks });

  const edit = useMutation({
    mutationFn: (draft: IssueDraft) =>
      api.updateTask(task.id, {
        title: draft.title,
        description: draft.description,
        type: draft.type,
        priority: draft.priority,
      }),
    onSuccess: () => {
      refresh();
      setEditOpen(false);
    },
    onError: () => toast({ tone: "warning", message: "Couldn't save the job." }),
  });

  // Status transitions through the existing task PATCH/close routes. A not-started job (§2)
  // offers Start now — PATCH to in_progress, which the shared source refresh re-groups from
  // Coming up into Doing now — and Close job, which closes it out of Work. A started job keeps
  // the base `TasksPanel` board moves (Mark open / Start, and Close); a closed one Reopens.
  // The routes succeed silently — the field table refreshes; a failure surfaces WorkDetail's
  // inline "Couldn't …" Alert, keeping the actions in reach (no undo for a start/close).
  const status = issueStatus(task);
  const isClosed = status === "closed";
  const isActive = status === "in_progress";
  const notStarted = jobNotStarted(item);

  const actions: WorkAction[] = [
    { id: "edit", label: "Edit", run: async () => setEditOpen(true) },
  ];
  if (notStarted) {
    actions.push({
      id: "start",
      label: "Start now",
      run: async () => {
        await api.updateTask(task.id, { status: "in_progress" });
        refresh();
      },
    });
    actions.push({
      id: "close",
      label: "Close job",
      run: async () => {
        await api.closeTask(task.id);
        refresh();
      },
    });
  } else if (!isClosed) {
    actions.push({
      id: "start",
      label: isActive ? "Mark open" : "Start",
      run: async () => {
        await api.updateTask(task.id, { status: isActive ? "open" : "in_progress" });
        refresh();
      },
    });
    // A job quiet for a day (ADR 0129) was likely finished elsewhere without a task_close:
    // its Close asks why (the reason rides the close route onto the record) and the chat
    // that created it is one step away. Not destructive — the confirm stays primary.
    const staleDays = taskStaleDays(task);
    if (staleDays != null) {
      actions.push({
        id: "close",
        label: "Close…",
        confirm: {
          prompt: "Close this job?",
          explanation: `${staleTaskMeta(staleDays)}. Say what happened to it — the note lands on the job's record.`,
          confirmLabel: "Close job",
          keepLabel: "Keep it",
          reason: { label: "Reason", placeholder: "Finished elsewhere, no longer needed…" },
        },
        run: async (reason) => {
          await api.closeTask(task.id, reason);
          refresh();
        },
      });
      if (openChat && task.session_id) {
        const sessionId = task.session_id;
        actions.push({ id: "open-chat", label: "Open chat", run: async () => openChat(sessionId) });
      }
    } else {
      actions.push({
        id: "close",
        label: "Close",
        run: async () => {
          await api.closeTask(task.id);
          refresh();
        },
      });
    }
  } else {
    actions.push({
      id: "reopen",
      label: "Reopen",
      run: async () => {
        await api.updateTask(task.id, { status: "open" });
        refresh();
      },
    });
  }
  actions.push(
    {
      id: "delete",
      label: "Delete…",
      destructive: true,
      confirm: {
        prompt: "Delete this job?",
        explanation: "It's removed from the board. You can undo this for a few seconds.",
        confirmLabel: "Delete",
        keepLabel: "Keep it",
      },
      run: async () => {
        // The bd-0ia51.5 delete mints a short-lived `undo_token` the API type doesn't
        // carry yet; read it off the response to drive the §4.5 undo toast.
        const res = (await api.deleteTask(task.id)) as { undo_token?: string };
        refresh();
        return { undoToken: res.undo_token };
      },
    },
  );

  return (
    <>
      {status === "waiting" ? <WaitingTaskDetail task={task} openChat={openChat} refresh={refresh}
        actions={actions.filter((action) => action.id === "edit" || action.id === "delete")} /> : <WorkDetail
        title={task.title}
        status={item.status}
        fields={taskFields(task)}
        actions={actions}
        technical={{ Id: task.id }}
        type="Job"
        onUndo={async (token) => { await api.undo(token); refresh(); }}
      />}
      <TaskCreateDialog
        open={editOpen}
        onClose={() => setEditOpen(false)}
        busy={edit.isPending}
        title="Edit job"
        submitLabel="Save changes"
        initial={taskDraft(task)}
        onCreate={(draft) => edit.mutate(draft)}
      />
    </>
  );
}

function TaskList({ source, onOpen }: { source: WorkSource; onOpen: (item: WorkItem) => void }) {
  if (source.status === "loading") {
    return <div aria-busy="true" aria-label="Loading jobs"><Skeleton lines={3} /></div>;
  }
  if (source.status === "error") {
    return (
      <Alert status="warning">
        Couldn&apos;t load jobs. The agent still has them. Only this view failed.{" "}
        <Button size="sm" onClick={source.retry}>Retry</Button>
      </Alert>
    );
  }
  // Close job visibly removes the row (jobShowsInWork — the groupFor rule the shared
  // workGroups counts apply).
  const items = source.items.filter(jobShowsInWork);
  if (!items.length) {
    return <Empty title="No jobs" description="The agent files one when it has work to track." />;
  }
  return (
    <>
      {source.status === "stale" && (
        <Callout tone="warning">
          Showing the last load. Couldn&apos;t refresh.{" "}
          <Button size="sm" onClick={source.retry}>Retry</Button>
        </Callout>
      )}
      <ul className="work-group">
        {items.map((item) => (
          <li key={`${item.kind}:${item.id}`}>
            <WorkRow
              status={item.status}
              tag="Job"
              icon={jobNotStarted(item) ? <CircleDashed size={16} className="work-row__icon" aria-hidden /> : undefined}
              title={item.title}
              meta={jobMeta(item)}
              focusKey={`${item.kind}:${item.id}`}
              onOpen={() => onOpen(item)}
            />
          </li>
        ))}
      </ul>
    </>
  );
}

/** One component for both panes — `state.itemId` selects detail vs list (WorkView §3.1). */
export function TasksSection({ sources, state, onOpen, decisionActions }: WorkSectionContext) {
  const source = sources.tasks;
  if (state.itemId) {
    const item = source.items.find(
      (candidate) => `${candidate.kind}:${candidate.id}` === state.itemId || candidate.id === state.itemId,
    );
    return item ? <TaskDetail key={item.id} item={item} openChat={decisionActions?.openSourceChat} /> : null;
  }
  return <TaskList source={source} onOpen={onOpen} />;
}

registerWorkSection("tasks", { label: "Tasks", group: "doing-now", render: (ctx) => <TasksSection {...ctx} /> });
