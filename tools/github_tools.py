"""GitHub read tools — PRs, issues, commits — over the ``gh`` CLI.

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

    return [github_get_pr, github_get_issue, github_list_issues, github_get_commit_diff]
