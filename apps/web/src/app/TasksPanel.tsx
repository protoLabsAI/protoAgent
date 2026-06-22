import { DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { Dialog } from "@protolabsai/ui/overlays";
import {
  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import {
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Loader2,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import { Suspense, useEffect, useState } from "react";

import { api } from "../lib/api";
import { onServerEvent } from "../lib/events";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { tasksQuery, queryKeys } from "../lib/queries";
import type { Task } from "../lib/types";
import {
  emptyIssueDraft,
  formatExactTimestamp,
  formatTimestamp,
  groupIssues,
  issueGroupId,
  issueStatus,
  issueStatusLabel,
  issueStatusTone,
  issueType,
  priorityLabel,
  type IssueDraft,
} from "./tasks";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { ScrollArea } from "@protolabsai/ui/data";
import { StatusPill } from "./StatusPill";

// The agent's task board (in-process tasks store), on the TanStack Query data
// layer (ADR 0013): the issue list is a `useSuspenseQuery` that invalidates on the
// `task.changed` bus push (the agent files/closes issues mid-turn) instead of
// polling; create/start/close/reopen/delete are `useMutation`s that invalidate it.
// The store is always initialized, so there's no init flow. Delete routes through
// the App-owned confirm dialog via the `confirm` prop.

type ConfirmRequest = {
  title: string;
  message?: string;
  confirmLabel?: string;
  onConfirm: () => void;
};

// Create a task from a dialog (opened by the panel's "New task" action) instead of
// an always-visible inline form — keeps the board the focus, with the full set of
// fields (title, type, priority, description) only when you're adding.
function TaskCreateDialog({
  open,
  onClose,
  onCreate,
  busy,
  error,
}: {
  open: boolean;
  onClose: () => void;
  onCreate: (draft: IssueDraft) => void;
  busy: boolean;
  error?: string | null;
}) {
  const [draft, setDraft] = useState<IssueDraft>(emptyIssueDraft);
  // Reset to a blank draft each time the dialog opens.
  useEffect(() => {
    if (open) setDraft(emptyIssueDraft);
  }, [open]);

  const canSubmit = !!draft.title.trim() && !busy;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={<><Boxes size={16} /> New task</>}
      width="min(520px, 94vw)"
      footer={
        <>
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button
            type="button"
            variant="primary"
            disabled={!canSubmit}
            data-testid="task-create-submit"
            onClick={() => onCreate(draft)}
          >
            {busy ? <Loader2 className="spin" size={16} /> : <Plus size={16} />} Create task
          </Button>
        </>
      }
    >
      <div className="task-create-form" data-testid="task-create-dialog">
        {error ? <p className="settings-status">Couldn't create: {error}</p> : null}
        <label className="field">
          <span>Title</span>
          <Input
            autoFocus
            value={draft.title}
            onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))}
            placeholder="What needs doing"
            data-testid="task-create-title"
          />
        </label>
        <div className="task-create-row">
          <label className="field">
            <span>Type</span>
            <DropdownSelect
              value={draft.type}
              onValueChange={(v) => setDraft((d) => ({ ...d, type: v }))}
              aria-label="Task type"
              options={[
                { value: "task", label: "task" },
                { value: "bug", label: "bug" },
                { value: "feature", label: "feature" },
                { value: "chore", label: "chore" },
                { value: "epic", label: "epic" },
              ]}
            />
          </label>
          <label className="field">
            <span>Priority</span>
            <DropdownSelect
              value={String(draft.priority)}
              onValueChange={(v) => setDraft((d) => ({ ...d, priority: Number(v) }))}
              aria-label="Task priority"
              options={[
                { value: "0", label: "P0" },
                { value: "1", label: "P1" },
                { value: "2", label: "P2" },
                { value: "3", label: "P3" },
                { value: "4", label: "P4" },
              ]}
            />
          </label>
        </div>
        <label className="field">
          <span>Description (optional)</span>
          <Textarea
            value={draft.description}
            rows={4}
            onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
            placeholder="Any detail the agent should have when it picks this up"
            data-testid="task-create-description"
          />
        </label>
      </div>
    </Dialog>
  );
}

function TasksBody({ confirm }: { confirm: (req: ConfirmRequest) => void }) {
  const { data } = useSuspenseQuery(tasksQuery());
  const issues = data.issues;
  const queryClient = useQueryClient();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.tasks });

  // Live: the agent created/closed/updated an issue mid-turn — refresh off the
  // `task.changed` bus push instead of polling every 5s (#1310), like the inbox panel.
  useEffect(() => onServerEvent("task.changed", invalidate), [queryClient]);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(["closed"]));

  const create = useMutation({
    mutationFn: (d: IssueDraft) =>
      api.createTask({
        title: d.title.trim(),
        type: d.type,
        priority: d.priority,
        description: d.description.trim() || undefined,
      }),
    onSuccess: () => setDialogOpen(false),
    onSettled: invalidate,
  });
  const update = useMutation({
    mutationFn: (v: { id: string; status: string }) => api.updateTask(v.id, { status: v.status }),
    onSettled: invalidate,
  });
  const close = useMutation({ mutationFn: (id: string) => api.closeTask(id), onSettled: invalidate });
  const remove = useMutation({ mutationFn: (id: string) => api.deleteTask(id), onSettled: invalidate });

  const busy = create.isPending || update.isPending || close.isPending || remove.isPending;

  const toggleGroup = (status: string) =>
    setCollapsed((cur) => {
      const next = new Set(cur);
      next.has(status) ? next.delete(status) : next.add(status);
      return next;
    });

  const askDelete = (issue: Task) =>
    confirm({
      title: `Delete ${issue.id}?`,
      message: `${issue.title ? `"${issue.title}"` : "This issue"} will be permanently deleted from the tasks store.`,
      confirmLabel: "Delete",
      onConfirm: () => remove.mutate(issue.id),
    });

  return (
    <>
      <PanelHeader
        compact
        title="Tasks"
        kicker="the agent's task board"
        actions={
          <Button variant="primary" type="button" onClick={() => setDialogOpen(true)} data-testid="task-new">
            <Plus size={16} /> New task
          </Button>
        }
      />

      <ScrollArea className="issue-list" role="region" aria-label="Tasks" tabIndex={0}>
        {issues.length === 0 ? (
          <Empty icon={<Boxes />} description="No tasks yet — add one with “New task”, or the agent will." />
        ) : (
          groupIssues(issues).map((group) => {
            const isGroupCollapsed = collapsed.has(group.status);
            const groupBodyId = issueGroupId(group.status);
            return (
              <section className={`issue-group${isGroupCollapsed ? " collapsed" : ""}`} key={group.status}>
                <div className="issue-group-header">
                  <button
                    className="issue-group-toggle"
                    type="button"
                    aria-expanded={!isGroupCollapsed}
                    aria-controls={groupBodyId}
                    onClick={() => toggleGroup(group.status)}
                  >
                    {isGroupCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                    <span>{issueStatusLabel(group.status)}</span>
                  </button>
                  <StatusPill label={String(group.issues.length)} tone="muted" />
                </div>
                {!isGroupCollapsed ? (
                  <div className="issue-group-body" id={groupBodyId}>
                    {group.issues.map((issue) => {
                      const status = issueStatus(issue);
                      const isClosed = status === "closed";
                      const isActive = status === "in_progress";
                      const createdLabel = formatTimestamp(issue.created_at);
                      const createdTitle = formatExactTimestamp(issue.created_at);
                      return (
                        <div className="issue-row" key={issue.id}>
                          <div className="issue-main">
                            <div className="issue-titleline">
                              <strong>{issue.title}</strong>
                            </div>
                            <div className="issue-toolbar">
                              <div className="issue-badges">
                                <span>{issue.id}</span>
                                <span>{issueType(issue)}</span>
                                <span>{priorityLabel(issue.priority)}</span>
                                {createdLabel ? (
                                  <span className="issue-time" title={createdTitle ? `Created ${createdTitle}` : "Created"}>
                                    created {createdLabel}
                                  </span>
                                ) : null}
                                {issue.assignee ? <span>{issue.assignee}</span> : null}
                                <StatusPill label={issueStatusLabel(status)} tone={issueStatusTone(status)} />
                              </div>
                              <div className="issue-actions">
                                {!isClosed ? (
                                  <Button icon variant="ghost"
                                    type="button"
                                    onClick={() => update.mutate({ id: issue.id, status: isActive ? "open" : "in_progress" })}
                                    disabled={busy}
                                    title={isActive ? "Mark open" : "Start issue"}
                                  >
                                    {isActive ? <CircleAlert size={15} /> : <Play size={15} />}
                                  </Button>
                                ) : null}
                                <Button icon variant="ghost"
                                  type="button"
                                  onClick={() =>
                                    isClosed ? update.mutate({ id: issue.id, status: "open" }) : close.mutate(issue.id)
                                  }
                                  disabled={busy}
                                  title={isClosed ? "Reopen issue" : "Close issue"}
                                >
                                  {isClosed ? <Play size={15} /> : <CheckCircle2 size={15} />}
                                </Button>
                                <Button icon variant="danger"
                                  type="button"
                                  onClick={() => askDelete(issue)}
                                  disabled={busy}
                                  title="Delete issue"
                                >
                                  <Trash2 size={15} />
                                </Button>
                              </div>
                            </div>
                            {issue.description ? (
                              <details className="issue-description-block">
                                <summary>Description</summary>
                                <p className="issue-description">{issue.description}</p>
                              </details>
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </section>
            );
          })
        )}
      </ScrollArea>

      <TaskCreateDialog
        open={dialogOpen}
        onClose={() => { setDialogOpen(false); create.reset(); }}
        onCreate={(d) => create.mutate(d)}
        busy={create.isPending}
        error={create.isError ? (create.error as Error).message : null}
      />
    </>
  );
}

export function TasksPanel({ confirm }: { confirm: (req: ConfirmRequest) => void }) {
  return (
    <section className="panel side-panel tasks-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="tasks" />}>
            <Suspense fallback={<PanelSkeleton label="Loading tasks…" />}>
              <TasksBody confirm={confirm} />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
