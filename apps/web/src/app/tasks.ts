// Tasks issue helpers — shared by the TasksPanel and (historically) App.
// Pure presentation/derivation logic over the Task shape.

import type { Task } from "../lib/types";
import type { StatusTone } from "./StatusPill";

export { formatTimestamp } from "../lib/format";

export type IssueDraft = {
  title: string;
  description: string;
  type: string;
  priority: number;
};

export const emptyIssueDraft: IssueDraft = {
  title: "",
  description: "",
  type: "task",
  priority: 2,
};

const issueStatusOrder = ["in_progress", "open", "blocked", "deferred", "closed"];

export function issueStatus(issue: Task) {
  return issue.status || "open";
}

export function issueType(issue: Task) {
  return issue.issue_type || issue.type || "task";
}

export function issueStatusLabel(status: string) {
  return status.replace(/_/g, " ");
}

export function issueStatusTone(status: string): StatusTone {
  if (status === "closed") return "success";
  if (status === "blocked") return "error";
  if (status === "in_progress" || status === "deferred") return "warning";
  return "muted";
}

export function priorityLabel(priority: Task["priority"]) {
  if (priority === undefined || priority === null || priority === "") return "P-";
  const value = String(priority);
  return value.toUpperCase().startsWith("P") ? value.toUpperCase() : `P${value}`;
}

function parseTimestamp(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatExactTimestamp(value?: string | null) {
  const date = parseTimestamp(value);
  if (!date) return "";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "full",
    timeStyle: "long",
  }).format(date);
}

export function issueGroupId(status: string) {
  return `issue-group-${status.replace(/[^a-z0-9_-]/gi, "-")}`;
}

export function groupIssues(issues: Task[]) {
  const buckets = new Map<string, Task[]>();
  for (const issue of issues) {
    const status = issueStatus(issue);
    const bucket = buckets.get(status);
    if (bucket) {
      bucket.push(issue);
    } else {
      buckets.set(status, [issue]);
    }
  }

  const ordered = issueStatusOrder.filter((status) => buckets.has(status));
  const rest = [...buckets.keys()].filter((status) => !issueStatusOrder.includes(status)).sort();
  return [...ordered, ...rest].map((status) => ({
    status,
    issues: buckets.get(status) || [],
  }));
}
