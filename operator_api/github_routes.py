"""GitHub issue creation for the console — the write counterpart to the read-only
``github`` plugin tools.

Backs the **New issue** form dialog (the console UX for the user-only ``/issue``
command). ``POST /api/github/issue`` files an issue through
``tools.gh_issue.file_issue`` — the *same* gate-conformance check + ``gh`` path the
chat ``/issue`` command uses — so the dialog and the command can never diverge.
``GET /api/github/config`` gives the dialog its prefilled defaults.

This is operator-surface only; it is deliberately not exposed to the agent as a
tool (creating issues stays user-driven).
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.server")


def _default_repo() -> str:
    """The configured default repo (``github.default_repo``), or ``""``."""
    from runtime.state import STATE

    cfg = getattr(STATE, "graph_config", None)
    return getattr(cfg, "github_default_repo", "") or ""


def _repos() -> list[str]:
    """The configured repo picker list (``github.repos``)."""
    from runtime.state import STATE

    cfg = getattr(STATE, "graph_config", None)
    return [str(r).strip() for r in (getattr(cfg, "github_repos", []) or []) if str(r).strip()]


def register_github_routes(app) -> None:
    import re
    import shutil

    from fastapi import Body

    from tools.gh_issue import IssueRequest, effective_default_repo, file_issue, labels_for, resolve_repo

    _REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

    @app.get("/api/github/config")
    async def _github_config():
        """Defaults the New-issue dialog prefills: the repo picker list, the
        effective default repo (default ⊕ first-in-list ⊕ env), and whether the
        ``gh`` CLI is installed."""
        repos = _repos()
        return {
            "repos": repos,
            "default_repo": resolve_repo(None, effective_default_repo(_default_repo(), repos)) or "",
            "gh_available": shutil.which("gh") is not None,
        }

    @app.post("/api/github/issue")
    async def _github_create_issue(body: dict = Body(...)):
        """File a GitHub issue from the console form. Returns the structured
        ``file_issue`` result (``{ok, url|missing|error, ...}``)."""
        kind = (body.get("kind") or "generic").lower()
        if kind not in ("bug", "feature", "generic"):
            kind = "generic"
        title = (body.get("title") or "").strip()
        issue_body = (body.get("body") or "").strip()
        repo = resolve_repo(body.get("repo"), effective_default_repo(_default_repo(), _repos()))
        labels = labels_for(kind, [str(x) for x in (body.get("labels") or [])])
        dry_run = bool(body.get("dry_run"))

        if not title:
            return {"ok": False, "error": "Title is required."}
        if not repo:
            return {"ok": False, "error": "No target repo — set one in Settings ▸ GitHub, or enter one."}
        if not _REPO_RE.match(repo):
            return {"ok": False, "error": f"Repo must be 'owner/name' (got {repo!r})."}

        return await file_issue(
            IssueRequest(title=title, body=issue_body, kind=kind, repo=repo, labels=labels, dry_run=dry_run)
        )
