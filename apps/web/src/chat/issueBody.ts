// Pure helpers for the New-issue dialog — no React, so they're unit-testable
// without a DOM. The body is assembled with the exact `##` headings the server
// gate checks for, so a dialog-filed issue always conforms (the same rules
// tools.gh_issue / .github/workflows/issue-gate.yml enforce).

export type Kind = "bug" | "feature";

export type Fields = {
  problem: string;
  repro: string;
  expected: string;
  proposal: string;
  acceptance: string;
  refs: string;
};

export const EMPTY_FIELDS: Fields = {
  problem: "",
  repro: "",
  expected: "",
  proposal: "",
  acceptance: "",
  refs: "",
};

export function buildBody(kind: Kind, f: Fields): string {
  const parts = [`## Problem\n${f.problem.trim()}`];
  if (kind === "bug") {
    parts.push(`## Steps to reproduce / evidence\n${f.repro.trim()}`);
    parts.push(`## Expected vs. actual\n${f.expected.trim()}`);
  } else {
    parts.push(`## Proposed direction\n${f.proposal.trim()}`);
  }
  parts.push(`## Acceptance\n${f.acceptance.trim()}`);
  if (f.refs.trim()) parts.push(`## Refs\n${f.refs.trim()}`);
  return parts.join("\n\n");
}

// Mirror the gate's required sections so the dialog can enable submit only when
// the issue will pass (the server stays the source of truth on submit).
export function isComplete(kind: Kind, title: string, repo: string, f: Fields): boolean {
  if (!title.trim() || !repo.trim()) return false;
  if (!f.problem.trim() || !f.acceptance.trim()) return false;
  if (kind === "bug") return !!f.repro.trim() && !!f.expected.trim();
  return !!f.proposal.trim();
}
