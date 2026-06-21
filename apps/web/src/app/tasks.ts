// Tasks issue helpers — shared by the TasksPanel and (historically) App.
// Pure presentation/derivation logic over the Task shape.

import type { Task } from "../lib/types";
import type { StatusTone } from "./StatusPill";

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

function dayStart(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

export function formatTimestamp(value?: string | null) {
  const date = parseTimestamp(value);
  if (!date) return "";

  const now = new Date();
  const dayDelta = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  const time = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);

  if (dayDelta === 0) return `today at ${time}`;
  if (dayDelta === 1) return `yesterday at ${time}`;

  const options: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  if (date.getFullYear() !== now.getFullYear()) {
    options.year = "numeric";
  }
  return new Intl.DateTimeFormat(undefined, options).format(date);
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
