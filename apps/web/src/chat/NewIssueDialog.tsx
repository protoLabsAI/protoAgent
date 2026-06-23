import { DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Dialog } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { Bug, Github, Loader2, Send, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import { buildBody, EMPTY_FIELDS, type Fields, isComplete, type Kind } from "./issueBody";

// Sentinel option that switches the repo picker to a free-text field for a
// one-off repo not in the configured list.
const CUSTOM_REPO = "__custom__";

/**
 * The console UX for the user-only `/issue` command: a form that files a GitHub
 * issue via POST /api/github/issue (which shares the server's gate-conformance +
 * `gh` path with the chat command). Opened from the composer by picking `/issue`.
 */
export function NewIssueDialog({
  open,
  onClose,
  onFiled,
}: {
  open: boolean;
  onClose: () => void;
  onFiled: (note: string) => void;
}) {
  const [kind, setKind] = useState<Kind>("bug");
  const [repo, setRepo] = useState("");
  const [title, setTitle] = useState("");
  const [fields, setFields] = useState<Fields>(EMPTY_FIELDS);
  const [repos, setRepos] = useState<string[]>([]);
  const [defaultRepo, setDefaultRepo] = useState("");
  const [customRepo, setCustomRepo] = useState(false);
  const [ghAvailable, setGhAvailable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset + prefill (repo list, default, gh availability) each time it opens.
  useEffect(() => {
    if (!open) return;
    setKind("bug");
    setTitle("");
    setFields(EMPTY_FIELDS);
    setError(null);
    setBusy(false);
    api
      .githubConfig()
      .then((c) => {
        setRepos(c.repos);
        setDefaultRepo(c.default_repo);
        setRepo(c.default_repo || c.repos[0] || "");
        // No configured repos at all → start in free-text mode.
        setCustomRepo(c.repos.length === 0 && !c.default_repo);
        setGhAvailable(c.gh_available);
      })
      .catch(() => {});
  }, [open]);

  // Dropdown options: the default first, then the configured list, de-duped.
  const repoOptions = useMemo(() => {
    const out: string[] = [];
    if (defaultRepo) out.push(defaultRepo);
    for (const r of repos) if (r && !out.includes(r)) out.push(r);
    return out;
  }, [repos, defaultRepo]);

  const set = (k: keyof Fields) => (e: { target: { value: string } }) =>
    setFields((f) => ({ ...f, [k]: e.target.value }));

  // Enable submit only when the issue will clear the gate (server stays source
  // of truth on submit). `isComplete` mirrors the gate's required sections.
  const canSubmit = useMemo(
    () => !busy && isComplete(kind, title, repo, fields),
    [title, repo, busy, fields, kind],
  );

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.createIssue({
        title: title.trim(),
        body: buildBody(kind, fields),
        kind,
        repo: repo.trim() || undefined,
      });
      if (!res.ok) {
        setError(res.error || (res.missing ? `Missing: ${res.missing.join("; ")}` : "Couldn't file the issue."));
        return;
      }
      onFiled(`✓ Filed in ${res.repo ?? repo}: ${res.url ?? "(created)"}`);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={
        <>
          <Github size={16} /> New GitHub issue
        </>
      }
      width="min(560px, 94vw)"
      footer={
        <>
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            disabled={!canSubmit}
            data-testid="issue-create-submit"
            onClick={submit}
          >
            {busy ? <Loader2 className="spin" size={16} /> : <Send size={16} />} File issue
          </Button>
        </>
      }
    >
      <div className="task-create-form" data-testid="issue-create-dialog">
        {error ? <p className="settings-status field-warn">{error}</p> : null}
        {!ghAvailable ? (
          <p className="field-hint field-warn">
            <Bug size={12} /> The <code>gh</code> CLI isn't installed on the host — filing will fail until it is.
          </p>
        ) : null}
        <div className="task-create-row">
          <label className="field">
            <span>Type</span>
            <DropdownSelect
              value={kind}
              onValueChange={(v) => setKind(v as Kind)}
              aria-label="Issue type"
              options={[
                { value: "bug", label: "Bug" },
                { value: "feature", label: "Enhancement" },
              ]}
            />
          </label>
          <label className="field">
            <span>Repo (owner/name)</span>
            {repoOptions.length > 0 && !customRepo ? (
              <DropdownSelect
                value={repo}
                onValueChange={(v) => {
                  if (v === CUSTOM_REPO) {
                    setCustomRepo(true);
                    setRepo("");
                  } else {
                    setRepo(v);
                  }
                }}
                aria-label="Repo"
                options={[
                  ...repoOptions.map((r) => ({ value: r, label: r })),
                  { value: CUSTOM_REPO, label: "Custom…" },
                ]}
              />
            ) : repoOptions.length > 0 ? (
              // Custom mode (a list exists): free-text + an inline × to return to
              // the dropdown — kept on the same row so the field doesn't shift.
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Input
                  autoFocus
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  placeholder="owner/name"
                  data-testid="issue-create-repo"
                  style={{ flex: 1 }}
                />
                <button
                  type="button"
                  aria-label="Back to repo list"
                  title="Back to repo list"
                  onClick={() => {
                    setCustomRepo(false);
                    setRepo(defaultRepo || repos[0] || "");
                  }}
                  style={{
                    display: "inline-flex",
                    background: "none",
                    border: "none",
                    padding: 4,
                    cursor: "pointer",
                    color: "var(--fg-muted)",
                  }}
                >
                  <X size={14} />
                </button>
              </div>
            ) : (
              // No configured list at all → plain free-text (nothing to go back to).
              <Input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder={defaultRepo || "owner/name"}
                data-testid="issue-create-repo"
              />
            )}
          </label>
        </div>
        <label className="field">
          <span>Title</span>
          <Input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={kind === "bug" ? "What's broken, in one line" : "The capability, in one line"}
            data-testid="issue-create-title"
          />
        </label>
        <label className="field">
          <span>{kind === "bug" ? "Problem / what's wrong" : "Problem / motivation"}</span>
          <Textarea value={fields.problem} rows={3} onChange={set("problem")} placeholder="Why this matters, and where" />
        </label>
        {kind === "bug" ? (
          <>
            <label className="field">
              <span>Steps to reproduce / evidence</span>
              <Textarea value={fields.repro} rows={3} onChange={set("repro")} placeholder="Minimal steps, logs, trace" />
            </label>
            <label className="field">
              <span>Expected vs. actual</span>
              <Textarea value={fields.expected} rows={2} onChange={set("expected")} placeholder="Expected … / got …" />
            </label>
          </>
        ) : (
          <label className="field">
            <span>Proposed direction</span>
            <Textarea
              value={fields.proposal}
              rows={3}
              onChange={set("proposal")}
              placeholder="Sketch the approach; note trade-offs"
            />
          </label>
        )}
        <label className="field">
          <span>Acceptance</span>
          <Textarea
            value={fields.acceptance}
            rows={2}
            onChange={set("acceptance")}
            placeholder="Verifiable criteria for done"
          />
        </label>
        <label className="field">
          <span>Refs (optional)</span>
          <Input value={fields.refs} onChange={set("refs")} placeholder="#1300, ADR 0047" />
        </label>
      </div>
    </Dialog>
  );
}
