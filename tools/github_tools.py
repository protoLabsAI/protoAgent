"""GitHub read tools — PRs, issues, commits, CI runs/failures — over the ``gh`` CLI.

Closes the long-standing fleet requests for GitHub read access
(protoAgent #158, #159). Each tool requires an explicit ``repo``
(``owner/name``) — there is deliberately **no silent default**: an agent
that forgets ``repo`` should get an error, not have the call quietly fire
at the wrong repository (a real misrouting bug observed across the fleet).

Tools degrade gracefully: if ``gh`` isn't installed or auth is missing,
they return a readable ``Error: ...`` string the model can act on.

Wire-in: ``get_github_tools()`` is appended by ``tools/lg_tools.get_all_tools``.
"""

from __future__ import annotations

import json
import re

from langchain_core.tools import tool

from tools.fallbacks import with_fallback
from tools.gh_cli import check_gh_error, run_gh

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Error-relevant lines to surface from a failed CI log (github_run_failure) — the
# deterministic version of hand-grepping a CI log for what actually broke.
_CI_ERR_RE = re.compile(
    r"(error|fail|✕|✗|×|not ok|exit code|command not found|exception|traceback|"
    r"assertion|timeout|expected .* to|cannot |refused|unauthorized|forbidden|"
    r"panic|fatal)",
    re.IGNORECASE,
)


def _bad_repo(repo: str) -> str | None:
    if not repo or not _REPO_RE.match(repo):
        return (
            f"Error: 'repo' must be 'owner/name' (got {repo!r}). "
            "Pass it explicitly — there is no default repository."
        )
    return None


def get_github_tools() -> list:
    """Return the GitHub read tools. Safe to include unconditionally — they
    return an error string when ``gh``/auth is unavailable."""

    @tool
    @with_fallback()
    async def github_get_pr(repo: str, number: int) -> str:
        """Fetch a GitHub pull request: title, state, author, body, and changed files.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            number: PR number.
        """
        err = _bad_repo(repo)
        if err:
            return err
        rc, out, serr = await run_gh([
            "pr", "view", str(number), "--repo", repo,
            "--json", "number,title,state,author,body,additions,deletions,files,url",
        ])
        gh_err = check_gh_error(rc, serr)
        if gh_err:
            return gh_err
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        files = ", ".join(f.get("path", "?") for f in (d.get("files") or [])[:20])
        return (
            f"PR #{d.get('number')} [{d.get('state')}] {d.get('title')}\n"
            f"by {(d.get('author') or {}).get('login', '?')} | "
            f"+{d.get('additions', 0)}/-{d.get('deletions', 0)} | {d.get('url')}\n"
            f"files: {files or '(none)'}\n\n{(d.get('body') or '').strip()[:2000]}"
        )

    @tool
    @with_fallback()
    async def github_get_issue(repo: str, number: int) -> str:
        """Fetch a GitHub issue: title, state, author, labels, and body.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            number: Issue number.
        """
        err = _bad_repo(repo)
        if err:
            return err
        rc, out, serr = await run_gh([
            "issue", "view", str(number), "--repo", repo,
            "--json", "number,title,state,author,labels,body,url",
        ])
        gh_err = check_gh_error(rc, serr)
        if gh_err:
            return gh_err
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        labels = ", ".join(lbl.get("name", "") for lbl in (d.get("labels") or []))
        return (
            f"Issue #{d.get('number')} [{d.get('state')}] {d.get('title')}\n"
            f"by {(d.get('author') or {}).get('login', '?')} | labels: {labels or '(none)'} | "
            f"{d.get('url')}\n\n{(d.get('body') or '').strip()[:2000]}"
        )

    @tool
    @with_fallback()
    async def github_list_issues(repo: str, state: str = "open", limit: int = 20) -> str:
        """List GitHub issues for a repo (closes #158).

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            state: ``open`` | ``closed`` | ``all`` (default ``open``).
            limit: Max issues to return (1–50, default 20).
        """
        err = _bad_repo(repo)
        if err:
            return err
        if state not in ("open", "closed", "all"):
            return f"Error: state must be open|closed|all (got {state!r})."
        limit = max(1, min(int(limit), 50))
        rc, out, serr = await run_gh([
            "issue", "list", "--repo", repo, "--state", state, "--limit", str(limit),
            "--json", "number,title,state,labels",
        ])
        gh_err = check_gh_error(rc, serr)
        if gh_err:
            return gh_err
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        if not items:
            return f"No {state} issues in {repo}."
        lines = [f"{len(items)} {state} issue(s) in {repo}:"]
        for it in items:
            labels = ",".join(lbl.get("name", "") for lbl in (it.get("labels") or []))
            lines.append(f"  #{it.get('number')} [{it.get('state')}] {it.get('title')}"
                         + (f"  ({labels})" if labels else ""))
        return "\n".join(lines)

    @tool
    @with_fallback()
    async def github_get_commit_diff(repo: str, ref: str, max_chars: int = 8000) -> str:
        """Fetch a commit's metadata + unified diff (closes #159).

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            ref: Commit SHA (or ref) to inspect.
            max_chars: Truncate the diff at this many characters (default 8000).
        """
        err = _bad_repo(repo)
        if err:
            return err
        # The diff media type returns a raw unified diff via the REST API.
        rc, out, serr = await run_gh([
            "api", f"repos/{repo}/commits/{ref}",
            "-H", "Accept: application/vnd.github.diff",
        ])
        gh_err = check_gh_error(rc, serr)
        if gh_err:
            return gh_err
        diff = out.strip()
        if not diff:
            return f"No diff for {repo}@{ref} (empty or merge commit)."
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n… (truncated at {max_chars} chars)"
        return f"Commit {repo}@{ref}:\n\n{diff}"

    @tool
    @with_fallback()
    async def github_ci_runs(repo: str, branch: str = "", limit: int = 15) -> str:
        """List recent GitHub Actions runs for a repo — for CI triage.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            branch: Optional branch filter (e.g. ``main``).
            limit: Max runs to return (capped at 50).

        Feed a failing run's id to ``github_run_failure`` to see why it failed.
        """
        err = _bad_repo(repo)
        if err:
            return err
        args = [
            "run", "list", "--repo", repo,
            "--limit", str(max(1, min(int(limit), 50))),
            "--json", "databaseId,name,status,conclusion,headBranch,event,createdAt,url",
        ]
        if branch.strip():
            args += ["--branch", branch.strip()]
        rc, out, serr = await run_gh(args)
        gh_err = check_gh_error(rc, serr)
        if gh_err:
            return gh_err
        try:
            runs = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        if not runs:
            return f"No recent runs for {repo}" + (f" on {branch}" if branch.strip() else "")
        lines = [
            f"#{r.get('databaseId')} [{r.get('conclusion') or r.get('status')}] "
            f"{r.get('name')} ({r.get('headBranch')} · {r.get('event')}) — {r.get('url')}"
            for r in runs
        ]
        return f"{repo} — {len(runs)} recent run(s):\n" + "\n".join(lines)

    @tool
    @with_fallback()
    async def github_run_failure(repo: str, run_id: int, max_lines: int = 40) -> str:
        """Explain why a GitHub Actions run failed — the error lines from its
        failed steps (the deterministic version of hand-grepping a CI log).

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            run_id: The run id (``databaseId`` from ``github_ci_runs``).
            max_lines: Cap on error lines returned (capped at 80).

        Pulls only the failed steps' logs (``gh run view --log-failed``), keeps the
        error-relevant lines (matched, deduped), and falls back to the log tail
        when nothing matches.
        """
        err = _bad_repo(repo)
        if err:
            return err
        cap = max(5, min(int(max_lines), 80))
        rc, out, serr = await run_gh(
            ["run", "view", str(run_id), "--repo", repo, "--log-failed"], timeout=60
        )
        gh_err = check_gh_error(rc, serr)
        if gh_err:
            return gh_err
        # gh prefixes each line "<job>\t<step>\t<timestamp> <message>"; keep the tail.
        raw = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        seen: set = set()
        uniq: list[str] = []
        for ln in raw:
            msg = ln.split("\t")[-1]
            if _CI_ERR_RE.search(msg):
                key = msg[:120]
                if key not in seen:
                    seen.add(key)
                    uniq.append(msg[:200])
        picked = uniq[-cap:] if uniq else [ln.split("\t")[-1][:200] for ln in raw[-cap:]]
        if not picked:
            return (
                f"Run {run_id} in {repo}: no failed-step log lines "
                "(run may not have failed, or its logs expired)."
            )
        return f"{repo} run {run_id} — failure log ({len(picked)} line(s)):\n" + "\n".join(picked)

    return [
        github_get_pr,
        github_get_issue,
        github_list_issues,
        github_get_commit_diff,
        github_ci_runs,
        github_run_failure,
    ]
