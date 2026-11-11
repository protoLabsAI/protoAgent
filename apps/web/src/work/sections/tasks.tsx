/**
 * Work view ▸ Tasks section (#4955 §2.7).
 *
 * Registers itself on import. One component renders both panes of the Work view: the list
 * of `WorkRow`s (when no item is open) and the `WorkDetail` for the open item — told apart
 * by `state.itemId`, the way the shell drives them (WorkView §3.1).
 *
 * The detail shows §2.7's fields: type and priority as "Bug · high" (numeric priority 0–3
 * reads urgent / high / normal / low), assignee, updated/closed times, a clamped
 * description and the created date. The only "Never shown" field, the id, lives in
 * Technical details. Actions mirror the base `TasksPanel` board controls: the Start /
 * Mark open and Close / Reopen status transitions (the PATCH/close routes), Edit (the
 * existing PATCH, reusing `TaskCreateDialog`) and Delete…, which hands back an
 * `undo_token` (bd-0ia51.5) driving the §4.5 undo toast.
 */
import { useState, type CSSProperties } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { Button, Callout, Empty } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";

import { api } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import type { Task } from "../../lib/types";
import { TaskCreateDialog } from "../../app/TasksPanel";
import { issueStatus, issueType, type IssueDraft } from "../../app/tasks";
import { relTime } from "../tone";
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
  const [open, setOpen] = useState(false);
  const long = text.split("\n").length > 8 || text.length > 400;
  if (!long) return <div className="work-task-desc" style={{ whiteSpace: "pre-wrap" }}>{text}</div>;
  return (
    <div>
      <div className="work-task-desc" style={open ? { whiteSpace: "pre-wrap" } : CLAMP_STYLE}>{text}</div>
      <Button variant="ghost" size="sm" onClick={() => setOpen(!open)}>
        {open ? "Show less" : "Show full text"}
      </Button>
    </div>
  );
}

function taskFields(task: Task): WorkField[] {
  const fields: WorkField[] = [{ label: "Type and priority", value: typeAndPriority(task) }];
  if (task.assignee) fields.push({ label: "Assignee", value: `Assigned to ${task.assignee}` });
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

function TaskDetail({ item }: { item: WorkItem }) {
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
    onError: () => toast({ tone: "warning", message: "Couldn't save the task." }),
  });

  // Status transitions, mirroring the base `TasksPanel` board buttons so the same moves are
  // reachable from the Work surface: an open/blocked task can be Started (→ in_progress) and an
  // in-progress one Marked open (→ open); any non-closed task Closed (the close route), and a
  // closed one Reopened (→ open). The PATCH/close succeed silently — the field table refreshes
  // to the new status; a failure surfaces WorkDetail's inline "Couldn't …" Alert (no undo).
  const status = issueStatus(task);
  const isClosed = status === "closed";
  const isActive = status === "in_progress";

  const actions: WorkAction[] = [
    { id: "edit", label: "Edit", run: async () => setEditOpen(true) },
  ];
  if (!isClosed) {
    actions.push({
      id: "start",
      label: isActive ? "Mark open" : "Start",
      run: async () => {
        await api.updateTask(task.id, { status: isActive ? "open" : "in_progress" });
        refresh();
      },
    });
  }
  actions.push({
    id: isClosed ? "reopen" : "close",
    label: isClosed ? "Reopen" : "Close",
    run: async () => {
      if (isClosed) await api.updateTask(task.id, { status: "open" });
      else await api.closeTask(task.id);
      refresh();
    },
  });
  actions.push(
    {
      id: "delete",
      label: "Delete…",
      destructive: true,
      confirm: {
        prompt: "Delete this task?",
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
      <WorkDetail
        title={task.title}
        status={item.status}
        fields={taskFields(task)}
        actions={actions}
        technical={{ Id: task.id }}
        type="Task"
        onUndo={async (token) => { await api.undo(token); refresh(); }}
      />
      <TaskCreateDialog
        open={editOpen}
        onClose={() => setEditOpen(false)}
        busy={edit.isPending}
        title="Edit task"
        submitLabel="Save changes"
        initial={taskDraft(task)}
        onCreate={(draft) => edit.mutate(draft)}
      />
    </>
  );
}

function TaskList({ source, onOpen }: { source: WorkSource; onOpen: (item: WorkItem) => void }) {
  if (source.status === "loading") {
    return <div aria-busy="true" aria-label="Loading tasks"><Skeleton lines={3} /></div>;
  }
  if (source.status === "error") {
    return (
      <Alert status="warning">
        Couldn&apos;t load tasks. The agent still has them. Only this view failed.{" "}
        <Button size="sm" onClick={source.retry}>Retry</Button>
      </Alert>
    );
  }
  if (!source.items.length) {
    return <Empty title="No tasks. The agent files one when it has work to track." />;
  }
  return (
    <>
      {source.status === "stale" && (
        <Callout tone="warning">
          Showing the last load. Couldn&apos;t refresh.{" "}
          <Button size="sm" onClick={source.retry}>Retry</Button>
        </Callout>
      )}
      {source.items.map((item) => (
        <WorkRow
          key={`${item.kind}:${item.id}`}
          status={item.status}
          title={item.title}
          meta={typeAndPriority(item.raw as Task)}
          focusKey={`${item.kind}:${item.id}`}
          onOpen={() => onOpen(item)}
        />
      ))}
    </>
  );
}

/** One component for both panes — `state.itemId` selects detail vs list (WorkView §3.1). */
export function TasksSection({ sources, state, onOpen }: WorkSectionContext) {
  const source = sources.tasks;
  if (state.itemId) {
    const item = source.items.find(
      (candidate) => `${candidate.kind}:${candidate.id}` === state.itemId || candidate.id === state.itemId,
    );
    return item ? <TaskDetail item={item} /> : null;
  }
  return <TaskList source={source} onOpen={onOpen} />;
}

registerWorkSection("tasks", { label: "Tasks", render: (ctx) => <TasksSection {...ctx} /> });
