"""``/issue`` — the user-only GitHub issue-creation control command.

This is the **write** counterpart to the read-only GitHub *tools*
(``tools/github_tools.py``). Those are agent-facing; creating an issue is a
write the agent must NOT do autonomously, so this is deliberately **not** a
LangChain tool. Instead it's a server-handled chat control command (like
``/goal``): the user types ``/issue …`` and the dispatcher short-circuits the
turn — the model never sees it as something it can call.

Syntax (inline, one message — a console form dialog is a planned follow-up)::

    /issue <title> [--bug|--feature] [--repo owner/name] [--label a,b] [--dry-run]

    ## Problem
    …
    ## Steps to reproduce / Acceptance
    …

The first line carries the title + flags; everything after the first newline is
the body. The body is checked against the SAME requirements the CI issue gate
enforces (`.github/workflows/issue-gate.yml`), so an issue filed here always
passes the gate:

- always: a non-trivial body + a Problem / What's-wrong / Motivation section;
- ``--bug``     → label ``bug``, also needs repro / evidence / expected-vs-actual;
- ``--feature`` → label ``enhancement``, also needs a proposed-direction or
  acceptance section.

Repo resolution (no silent misrouting): explicit ``--repo`` > the configured
``github.default_repo`` > ``GITHUB_DEFAULT_REPO`` / ``GH_REPO`` env > an error
asking for one. Auth rides on ``tools.gh_cli`` (``GITHUB_TOKEN``/``GH_TOKEN`` or
ambient ``gh auth``); ``gh issue create`` needs write scope.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

from tools.gh_cli import check_gh_error, run_gh

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Section detectors — kept in lockstep with the CI gate's regexes so the local
# check and the server-side gate can never disagree about what "conforms" means.
_SECTION_RES = {
    "problem": re.compile(r"problem|what'?s? wrong|motivation|background|context|summary", re.I),
    "repro": re.compile(r"repro|reproduce|steps|evidence|expected|actual|observed", re.I),
    "proposal": re.compile(r"propos|solution|approach|direction|fix|design", re.I),
    "acceptance": re.compile(r"acceptance|done when|success criteria|definition of done", re.I),
}
# A heading (#..######) or a bold line (**…**) — same shape the gate matches.
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+|\*\*\s*)(.+?)(?:\s*\*\*)?\s*$")

_BUG_SCAFFOLD = (
    "## Problem / what's wrong\n<what's broken, and where — name the file/subsystem>\n\n"
    "## Steps to reproduce / evidence\n<minimal steps, logs, or a stack trace>\n\n"
    "## Expected vs. actual\n<what you expected vs. what happened>\n\n"
    "## Acceptance\n<how we'll know it's fixed>\n"
)
_FEATURE_SCAFFOLD = (
    "## Problem / motivation\n<what gap or pain motivates this>\n\n"
    "## Proposed direction\n<sketch the approach; note trade-offs>\n\n"
    "## Acceptance\n<verifiable criteria for done>\n"
)
_GENERIC_SCAFFOLD = (
    "## Problem\n<what's wrong or what you want, and why it matters>\n\n"
    "## Acceptance\n<verifiable criteria for done>\n"
)


@dataclass
class IssueRequest:
    title: str
    body: str
    kind: str  # "bug" | "feature" | "generic"
    repo: str | None = None
    labels: list[str] = field(default_factory=list)
    dry_run: bool = False


def _has_section(body: str, key: str) -> bool:
    rx = _SECTION_RES[key]
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m and rx.search(m.group(1)):
            return True
    return False


def _scaffold(kind: str) -> str:
    return {"bug": _BUG_SCAFFOLD, "feature": _FEATURE_SCAFFOLD}.get(kind, _GENERIC_SCAFFOLD)


def missing_sections(body: str, kind: str) -> list[str]:
    """The gate-required sections absent from ``body`` for this issue ``kind``."""
    miss: list[str] = []
    # Same metric as the CI gate (collapsed-whitespace length >= 80), so an issue
    # that passes here is guaranteed to clear the server-side gate.
    if len(" ".join(body.split())) < 80:
        miss.append("a substantive description (>= 80 chars)")
    if not _has_section(body, "problem"):
        miss.append("a Problem / What's-wrong / Motivation section")
    if kind == "bug" and not _has_section(body, "repro"):
        miss.append("Steps to reproduce / Evidence / Expected-vs-actual")
    if kind == "feature" and not (_has_section(body, "proposal") or _has_section(body, "acceptance")):
        miss.append("a Proposed-direction or Acceptance section")
    return miss


def _parse(rest: str, *, default_repo: str = "") -> IssueRequest | str:
    """Parse the raw ``/issue`` argument string into a request, or an error string."""
    first, _, body = rest.partition("\n")
    body = body.strip()

    kind = "generic"
    labels: list[str] = []
    repo: str | None = None
    dry_run = False

    # Tokenize only the first line (title + flags); the body is taken verbatim.
    try:
        tokens = shlex.split(first)
    except ValueError:
        tokens = first.split()

    title_parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("--bug", "--fix"):
            kind = "bug"
        elif low in ("--feature", "--feat", "--enhancement"):
            kind = "feature"
        elif low in ("--dry-run", "--dryrun"):
            dry_run = True
        elif low.startswith("--repo"):
            val = tok.split("=", 1)[1] if "=" in tok else (tokens[i + 1] if i + 1 < len(tokens) else "")
            if "=" not in tok:
                i += 1
            repo = val.strip()
        elif low.startswith("--label"):
            val = tok.split("=", 1)[1] if "=" in tok else (tokens[i + 1] if i + 1 < len(tokens) else "")
            if "=" not in tok:
                i += 1
            labels += [s.strip() for s in val.split(",") if s.strip()]
        else:
            title_parts.append(tok)
        i += 1

    title = " ".join(title_parts).strip()
    if kind == "bug" and "bug" not in labels:
        labels.insert(0, "bug")
    if kind == "feature" and "enhancement" not in labels:
        labels.insert(0, "enhancement")

    repo = repo or default_repo or os.environ.get("GITHUB_DEFAULT_REPO") or os.environ.get("GH_REPO") or None

    if not title:
        return (
            "Usage: `/issue <title> [--bug|--feature] [--repo owner/name] [--dry-run]` "
            "then the body on the following lines.\n\n"
            "Scaffold to fill in:\n```\n" + _scaffold(kind) + "```"
        )
    if not repo:
        return (
            "No target repo. Pass `--repo owner/name`, or set `github.default_repo` "
            "in Settings (or the `GITHUB_DEFAULT_REPO` env var)."
        )
    if not _REPO_RE.match(repo):
        return f"Error: --repo must be 'owner/name' (got {repo!r})."

    return IssueRequest(title=title, body=body, kind=kind, repo=repo, labels=labels, dry_run=dry_run)


def is_issue_command(message: str) -> bool:
    """True when ``message`` is a ``/issue`` control command."""
    if not isinstance(message, str):
        return False
    s = message.strip().lower()
    return s == "/issue" or s.startswith("/issue ") or s.startswith("/issue\n")


async def parse_issue_control(message: str, *, default_repo: str = "") -> str | None:
    """Handle a ``/issue`` control message: validate, then create the issue (or
    report what's missing / what a dry-run would do). Returns the reply string
    when the message *was* an ``/issue`` command (caller short-circuits the
    turn), else ``None``."""
    if not is_issue_command(message):
        return None
    rest = message.strip()[len("/issue"):].strip()
    parsed = _parse(rest, default_repo=default_repo)
    if isinstance(parsed, str):
        return parsed  # usage / scaffold / validation error

    miss = missing_sections(parsed.body, parsed.kind)
    if miss:
        return (
            "Not filed — this issue is missing " + "; ".join(miss) + ".\n\n"
            "Add the section(s) and resend. Scaffold for a "
            f"{parsed.kind} issue:\n```\n" + _scaffold(parsed.kind) + "```"
        )

    label_note = f" · labels: {', '.join(parsed.labels)}" if parsed.labels else ""
    if parsed.dry_run:
        return (
            f"Dry run — would create in **{parsed.repo}**{label_note}:\n\n"
            f"**{parsed.title}**\n\n{parsed.body}"
        )

    args = ["issue", "create", "--repo", parsed.repo, "--title", parsed.title, "--body", parsed.body]
    for lbl in parsed.labels:
        args += ["--label", lbl]
    rc, out, serr = await run_gh(args, timeout=45)
    err = check_gh_error(rc, serr)
    if err:
        # A missing-label failure is the common one — surface it actionably.
        if "could not add label" in serr.lower() or "not found" in serr.lower():
            return f"{err}\n(Hint: the label may not exist in {parsed.repo}. Create it or drop `--label`.)"
        return err
    url = out.strip().splitlines()[-1] if out.strip() else ""
    return f"Filed in {parsed.repo}{label_note}: {url or '(created)'}"
