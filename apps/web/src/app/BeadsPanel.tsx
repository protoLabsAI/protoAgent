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
  Trash2,
} from "lucide-react";
import { Suspense, useState } from "react";

import { api } from "../lib/api";
import { PanelHeader } from "./PanelHeader";
import { beadsIssuesQuery, queryKeys } from "../lib/queries";
import type { BeadsIssue } from "../lib/types";
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
} from "./beads";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { ScrollArea } from "./ScrollArea";
import { StatusPill } from "./StatusPill";

// The agent's task board (in-process beads store), on the TanStack Query data
// layer (ADR 0013): the issue list is a `useSuspenseQuery` (refetching while
// mounted), create/start/close/reopen/delete are `useMutation`s that invalidate
// it. The store is always initialized, so there's no init flow. Delete routes
// through the App-owned confirm dialog via the `confirm` prop.

type ConfirmRequest = {
  title: string;
  message?: string;
  confirmLabel?: string;
  onConfirm: () => void;
};

function BeadsBody({ confirm }: { confirm: (req: ConfirmRequest) => void }) {
  const { data } = useSuspenseQuery(beadsIssuesQuery());
  const issues = data.issues;
  const queryClient = useQueryClient();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.beadsIssues });

  const [draft, setDraft] = useState<IssueDraft>(emptyIssueDraft);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(["closed"]));

  const create = useMutation({
    mutationFn: (d: IssueDraft) =>
      api.createIssue({
        title: d.title.trim(),
        type: d.type,
        priority: d.priority,
        description: d.description.trim() || undefined,
      }),
    onSuccess: () => setDraft(emptyIssueDraft),
    onSettled: invalidate,
  });
  const update = useMutation({
    mutationFn: (v: { id: string; status: string }) => api.updateIssue(v.id, { status: v.status }),
    onSettled: invalidate,
  });
  const close = useMutation({ mutationFn: (id: string) => api.closeIssue(id), onSettled: invalidate });
  const remove = useMutation({ mutationFn: (id: string) => api.deleteIssue(id), onSettled: invalidate });

  const busy = create.isPending || update.isPending || close.isPending || remove.isPending;

  const toggleGroup = (status: string) =>
    setCollapsed((cur) => {
      const next = new Set(cur);
      next.has(status) ? next.delete(status) : next.add(status);
      return next;
    });

  const askDelete = (issue: BeadsIssue) =>
    confirm({
      title: `Delete ${issue.id}?`,
      message: `${issue.title ? `"${issue.title}"` : "This issue"} will be permanently deleted from the beads store.`,
      confirmLabel: "Delete",
      onConfirm: () => remove.mutate(issue.id),
    });

  return (
    <>
      <form
        className="issue-create"
        onSubmit={(event) => {
          event.preventDefault();
          if (draft.title.trim()) create.mutate(draft);
        }}
      >
        <input
          value={draft.title}
          onChange={(event) => setDraft((d) => ({ ...d, title: event.target.value }))}
          placeholder="New issue title"
        />
        <button className="primary-button" type="submit" disabled={!draft.title.trim() || busy}>
          {create.isPending ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
          Add
        </button>
        <div className="issue-create-meta">
          <select
            value={draft.type}
            onChange={(event) => setDraft((d) => ({ ...d, type: event.target.value }))}
            aria-label="Issue type"
          >
            <option value="task">task</option>
            <option value="bug">bug</option>
            <option value="feature">feature</option>
            <option value="chore">chore</option>
          </select>
          <select
            value={draft.priority}
            onChange={(event) => setDraft((d) => ({ ...d, priority: Number(event.target.value) }))}
            aria-label="Issue priority"
          >
            <option value={0}>P0</option>
            <option value={1}>P1</option>
            <option value={2}>P2</option>
            <option value={3}>P3</option>
            <option value={4}>P4</option>
          </select>
          <input
            value={draft.description}
            onChange={(event) => setDraft((d) => ({ ...d, description: event.target.value }))}
            placeholder="Description"
          />
        </div>
        {create.isError ? (
          <p className="workflow-failed">{(create.error as Error).message}</p>
        ) : null}
      </form>

      <ScrollArea className="issue-list" ariaLabel="Beads tasks">
        {issues.length === 0 ? (
          <div className="empty-state stacked">
            <Boxes size={18} />
            <span>No issues yet — add one above, or the agent will.</span>
          </div>
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
                                  <button
                                    className="icon-button"
                                    type="button"
                                    onClick={() => update.mutate({ id: issue.id, status: isActive ? "open" : "in_progress" })}
                                    disabled={busy}
                                    title={isActive ? "Mark open" : "Start issue"}
                                  >
                                    {isActive ? <CircleAlert size={15} /> : <Play size={15} />}
                                  </button>
                                ) : null}
                                <button
                                  className="icon-button"
                                  type="button"
                                  onClick={() =>
                                    isClosed ? update.mutate({ id: issue.id, status: "open" }) : close.mutate(issue.id)
                                  }
                                  disabled={busy}
                                  title={isClosed ? "Reopen issue" : "Close issue"}
                                >
                                  {isClosed ? <Play size={15} /> : <CheckCircle2 size={15} />}
                                </button>
                                <button
                                  className="icon-button danger"
                                  type="button"
                                  onClick={() => askDelete(issue)}
                                  disabled={busy}
                                  title="Delete issue"
                                >
                                  <Trash2 size={15} />
                                </button>
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
    </>
  );
}

export function BeadsPanel({ confirm }: { confirm: (req: ConfirmRequest) => void }) {
  return (
    <section className="panel side-panel beads-panel">
      <PanelHeader compact title="Beads" kicker="the agent's task board" />
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="beads" />}>
            <Suspense fallback={<PanelSkeleton label="Loading beads…" />}>
              <BeadsBody confirm={confirm} />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
