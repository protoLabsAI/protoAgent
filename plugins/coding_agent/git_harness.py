"""Deterministic, framework-owned git lifecycle for managed acp delegates (ADR 0076).

The coder edits files and runs tests; this harness owns everything that touches a
branch, the remote, or the PR — the steps whose LLM ownership caused every observed
reliability failure (never-pushed work, duplicate PRs, branch collisions, committed
scratch). Ported from protoMaker's git workflow (source-verified in
docs/plans/coding-agent-deterministic-git.md), reshaped onto ``tools/shell.run_command``
+ ``tools/gh_cli.run_gh``.

Lifecycle (driven by ``AcpAdapter._dispatch_managed``):

    claim(item_id)                      # in-flight dedup — atomic on the event loop
    preflight_pr(...)                   # open PR already? return it, don't dispatch
    prepare(...)                        # fetch base, branch off origin/<base>, hygiene
    <coder runs, edit-only directive>
    finish(...)                         # guard → scan → commit → rebase → push → PR
    release(item_id)

"Edit-only" is an instruction, not an assumption: ``finish`` is idempotent to the coder
having done partial git itself (committed, or committed *and* pushed) — the three-tier
probe adopts that work instead of failing or duplicating it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field

from graph.middleware.redaction import PATTERNS as _REDACTION_PATTERNS
from tools.gh_cli import run_gh
from tools.shell import ShellResult, run_command

log = logging.getLogger("protoagent.plugins.coding_agent")

# ── deterministic identity ────────────────────────────────────────────────────


def derive_item_id(task: str) -> str:
    """Stable work-item id for a task with no caller-supplied one. Hashing the task
    text means a naive fan-out of the *same* task to several coders converges on one
    claim instead of N duplicate branches/PRs."""
    return hashlib.sha1(task.strip().encode()).hexdigest()[:12]


def _slugify(text: str, maxlen: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:maxlen].rstrip("-") or "task"


def mint_branch(prefix: str, task: str, item_id: str) -> str:
    """``<prefix>/<slug>-<last7(item_id)>`` — deterministic, no LLM, collision-proof
    via the id suffix (two items with the same title still get distinct branches)."""
    clean_prefix = re.sub(r"[^a-z0-9-]+", "-", prefix.strip().lower()).strip("-") or "task"
    first_line = next((ln for ln in task.strip().splitlines() if ln.strip()), "task")
    return f"{clean_prefix}/{_slugify(first_line)}-{item_id[-7:]}"


def title_from(task: str) -> str:
    """PR/commit title: the task's first non-empty line, collapsed, capped. The
    deterministic fallback for ``infer_title`` — fine when the caller's first line IS the
    intent, wrong when the task is wrapped in a coder preamble (see ``infer_title``)."""
    first_line = next((ln for ln in task.strip().splitlines() if ln.strip()), "automated change")
    title = " ".join(first_line.split())
    return title[:72].rstrip() if len(title) > 72 else title


_TITLE_PROMPT = (
    "You write ONE conventional-commit title summarizing the coding task below "
    "(e.g. `fix: guard null user`, `feat: add compare view`, `chore: bump deps`). "
    "Output ONLY the title: one line, no quotes, no body, <= 70 chars. IGNORE any "
    "boilerplate wrapped around the task — 'you are implementing one feature', "
    "working-directory notes, git/tooling rules; title the ACTUAL change the task asks for."
)


async def infer_title(task: str, config=None) -> str | None:
    """Infer a concise conventional-commit title from a task via the cheap aux model, so
    the branch slug + PR title describe the WORK — not whatever boilerplate preamble a
    caller wrapped the task in (project_board's coder prompt starts "You are implementing
    ONE feature in this repository…", which the naive first-line heuristic slugged into the
    branch/title). Best-effort: returns None on any failure so callers fall back to the
    deterministic ``title_from``. Uniqueness never depends on this — the branch's ``-<id7>``
    item-id suffix is what dedups (``preflight_pr``), so an inferred slug is free to vary."""
    task = (task or "").strip()
    if not task:
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from graph.agent import _resolve_aux_model
        from graph.llm import create_llm

        if config is None:
            from runtime.state import STATE

            config = getattr(STATE, "graph_config", None)
        if config is None:
            return None
        llm = create_llm(config, model_name=_resolve_aux_model(config, ""))
        resp = await llm.ainvoke(
            [SystemMessage(content=_TITLE_PROMPT), HumanMessage(content=task[:4000])]
        )
        raw = str(getattr(resp, "content", "") or "").strip()
        # Reject a chatty reply BEFORE collapsing whitespace (join-on-split would hide the
        # newline) — a real title is one short line; anything multi-line/long is the model
        # ignoring "one line", so fall back rather than slug a paragraph into a branch.
        if not raw or "\n" in raw or len(raw) > 100:
            return None
        title = " ".join(raw.split()).strip("\"'`").strip()
        return title[:72].rstrip() or None
    except Exception:  # noqa: BLE001 — inference is best-effort; the deterministic fallback covers it
        log.debug("[managed-git] title inference failed; using deterministic first-line", exc_info=True)
        return None


def edit_only_directive(branch: str) -> str:
    """Appended to the coder's task in managed mode."""
    return (
        "\n\n[managed git] Edit files and run tests only. Do NOT run git commands — do not "
        "branch, commit, push, or open a PR. The framework owns the git lifecycle: when you "
        f"finish, it commits your work and publishes it on branch `{branch}`."
    )


# ── single-claim registry (in-flight dedup) ───────────────────────────────────
#
# Module-global so every dispatch path (foreground tool call, background job) shares
# one registry. The check-and-set in ``claim`` is atomic because every caller is an
# async coroutine on the one event loop and there is NO await between the read and
# the write — protoMaker's exact trick. If a caller is ever sync/threaded, this needs
# a real lock.

_CLAIMS: dict[str, str] = {}


def claim(item_id: str, owner: str) -> str | None:
    """Claim ``item_id`` for ``owner``. Returns None on success, or the current
    holder's name if the item is already in flight. No await between check and set."""
    holder = _CLAIMS.get(item_id)
    if holder is not None:
        return holder
    _CLAIMS[item_id] = owner
    return None


def release(item_id: str) -> None:
    _CLAIMS.pop(item_id, None)


# ── git plumbing ──────────────────────────────────────────────────────────────

# Linked worktrees share one .git — parallel harness runs contend on its lock files.
# Bounded backoff-retry is the documented cure (the alternative is spurious failures
# under exactly the fan-out this feature exists for).
_LOCK_MARKERS = ("index.lock", "config.lock", "cannot lock ref", "unable to create")

# Committer identity injected when the environment has none (agent containers often
# don't) — commits would otherwise fail outright.
_FALLBACK_IDENTITY = {
    "GIT_AUTHOR_NAME": "protoAgent",
    "GIT_AUTHOR_EMAIL": "agent@protoagent.local",
    "GIT_COMMITTER_NAME": "protoAgent",
    "GIT_COMMITTER_EMAIL": "agent@protoagent.local",
}

# Scratch that must never reach a PR. Untracked scratch is excluded structurally
# (info/exclude, seeded by prepare); this list also backs a staged-path sweep in case
# the coder force-added any of it.
_SCRATCH_DIRS = (".proto/", ".worktrees/", ".claude/", "node_modules/")

# Reuse the maintained token-shape patterns from the audit-log redactor — one place
# to harden when providers change token formats. Only the token-SHAPED patterns:
# the redactor's contextual ones (bearer_token, generic_api_key, env_var_assignment,
# client_secret) match ordinary code (`API_KEY = os.environ[...]`) and would
# false-positive-block legitimate commits.
_TOKEN_PATTERN_NAMES = (
    "openai_key",
    "google_oauth",
    "google_api_key",
    "github_token",
    "slack_token",
    "aws_access_key",
    "discord_token",
)
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "github fine-grained token"),
    *((_REDACTION_PATTERNS[n], n.replace("_", " ")) for n in _TOKEN_PATTERN_NAMES if n in _REDACTION_PATTERNS),
]


async def _git(workdir: str, *args: str, env: dict[str, str] | None = None, timeout: float = 60.0) -> ShellResult:
    """Run one git command in ``workdir``, retrying on shared-.git lock contention."""
    for attempt in range(2):
        res = await run_command(["git", *args], cwd=workdir, env=env, timeout=timeout)
        blob = f"{res.stderr} {res.error or ''}".lower()
        if res.ok or not any(m in blob for m in _LOCK_MARKERS):
            return res
        await asyncio.sleep(0.4 * (2**attempt))
    return await run_command(["git", *args], cwd=workdir, env=env, timeout=timeout)


async def _count(workdir: str, spec: str) -> int | None:
    """``git rev-list --count <spec>`` → int, or None when unresolvable. None means
    *unknown* — callers must not treat it as 0, or work gets stranded (protoMaker's
    hard-learned rule)."""
    res = await _git(workdir, "rev-list", "--count", spec)
    if not res.ok:
        return None
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


async def _identity_env(workdir: str) -> dict[str, str] | None:
    """Fallback committer identity iff the repo/environment has none configured."""
    res = await _git(workdir, "config", "user.email")
    if res.ok and res.stdout.strip():
        return None
    return dict(_FALLBACK_IDENTITY)


async def _seed_exclude(workdir: str) -> None:
    """Structurally exclude scratch dirs via info/exclude (shared across linked
    worktrees — which is what we want). A pathspec denylist at ``git add`` time is
    deliberately NOT used: protoMaker tried and removed it (conflicts with tracked
    files under excluded dirs)."""
    res = await _git(workdir, "rev-parse", "--git-path", "info/exclude")
    if not res.ok:
        return
    path = res.stdout.strip()
    if not os.path.isabs(path):
        path = os.path.join(workdir, path)
    try:
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        missing = [d for d in _SCRATCH_DIRS if d not in existing]
        if missing:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n# managed-git scratch exclusions (ADR 0076)\n")
                f.writelines(f"{d}\n" for d in missing)
    except OSError as exc:  # noqa: PERF203 — exclusion is best-effort hygiene
        log.warning("[git-harness] seeding info/exclude failed: %s", exc)


def _scan_added_lines(diff_text: str) -> list[str]:
    """Secret findings in the diff's ADDED lines, as ``<file>: <kind>`` strings."""
    findings: list[str] = []
    current = "?"
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pattern, kind in _SECRET_PATTERNS:
            if pattern.search(line):
                finding = f"{current}: {kind}"
                if finding not in findings:
                    findings.append(finding)
    return findings


# ── pre-run: branch setup ─────────────────────────────────────────────────────


@dataclass
class PrepareResult:
    error: str | None = None
    notes: list[str] = field(default_factory=list)


async def prepare(workdir: str, *, base: str, branch: str) -> PrepareResult:
    """Put the delegate's worktree on ``branch``, cut from fresh ``origin/<base>``.

    Never branches off HEAD (a stale local base silently poisons every PR). A reused
    branch that has real commits ahead of base (a prior partial run) is kept; one with
    none is re-cut. Leftover uncommitted changes from a previous run are stashed —
    preserved but kept out of this item's commit."""
    out = PrepareResult()

    fetch = await _git(workdir, "fetch", "origin", base, timeout=120)
    if not fetch.ok:
        # Non-fatal: proceed on the cached ref (protoMaker's behavior) — but say so.
        out.notes.append(f"fetch origin/{base} failed ({(fetch.stderr or fetch.error or '?')[:120]}); using cached ref")

    dirty = await _git(workdir, "status", "--porcelain")
    if dirty.ok and dirty.stdout.strip():
        stash = await _git(workdir, "stash", "push", "-u", "-m", f"managed-git leftovers before {branch}")
        if stash.ok:
            out.notes.append("stashed leftover uncommitted changes from a previous run (`git stash list` to recover)")
        else:
            out.error = f"worktree has leftover changes and stashing them failed: {(stash.stderr or stash.error or '?')[:200]}"
            return out

    exists = (await _git(workdir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")).ok
    keep = False
    if exists:
        ahead = await _count(workdir, f"origin/{base}..{branch}")
        keep = ahead is None or ahead > 0  # unknown ⇒ keep — never discard possibly-real work
    if keep:
        co = await _git(workdir, "checkout", branch)
        out.notes.append(f"reusing branch {branch} (has commits from a prior run)")
    else:
        co = await _git(workdir, "checkout", "-B", branch, f"origin/{base}")
    if not co.ok:
        blob = f"{co.stderr} {co.error or ''}"
        if "already checked out" in blob or "already used by worktree" in blob:
            out.error = (
                f"branch {branch!r} is checked out in another worktree — another coder holds this "
                "item's branch. One branch per worktree; wait for it or prune the stale worktree."
            )
        else:
            out.error = f"checkout failed: {blob.strip()[:300]}"
        return out

    await _seed_exclude(workdir)
    return out


# ── post-run: commit → rebase → push → PR ─────────────────────────────────────


@dataclass
class GitOutcome:
    branch: str
    item_id: str
    base: str
    stranded_on_base: bool = False
    blocked_reason: str = ""  # non-base refusal (detached HEAD, unresolvable base, …)
    no_changes: bool = False
    coder_did_git: str = ""  # "" | "committed" | "pushed"
    committed: bool = False
    commit_sha: str = ""
    rebase_conflicts: list[str] = field(default_factory=list)
    pushed: bool = False
    pushed_sha: str = ""
    pr_url: str = ""
    pr_state: str = ""  # "created" | "existing" | "skipped-no-commits" | ""
    blocked_secrets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Operator/LLM-visible summary block appended to the coder's reply."""
        lines = [f"[managed git] item `{self.item_id}` on branch `{self.branch}` (base `{self.base}`)"]
        if self.stranded_on_base:
            lines.append(
                f"- BLOCKED: the coder left HEAD on `{self.base}` — nothing was committed. The edits "
                "are recoverable in the worktree. Do NOT report this item as done."
            )
        elif self.blocked_reason:
            lines.append(f"- BLOCKED: {self.blocked_reason} Do NOT report this item as done.")
        elif self.blocked_secrets:
            lines.append("- BLOCKED: commit refused, possible secrets in the diff: " + "; ".join(self.blocked_secrets))
        elif self.no_changes:
            lines.append("- no changes to publish (clean tree, nothing ahead of base)")
        else:
            if self.coder_did_git:
                lines.append(f"- adopted work the coder had already {self.coder_did_git}")
            if self.commit_sha:
                lines.append(f"- committed `{self.commit_sha[:10]}`")
            if self.rebase_conflicts:
                lines.append(
                    "- rebase onto fresh base hit conflicts (pushed as-is; resolve at merge time): "
                    + ", ".join(self.rebase_conflicts)
                )
            lines.append(
                f"- pushed to origin (remote verified at `{self.pushed_sha[:10]}`)" if self.pushed else "- NOT pushed"
            )
            if self.pr_url:
                lines.append(f"- PR ({self.pr_state}): {self.pr_url}")
            elif self.pr_state == "skipped-no-commits":
                lines.append("- PR skipped: branch has no commits ahead of base")
        lines.extend(f"- note: {e}" for e in self.errors)
        return "\n".join(lines)


async def finish(workdir: str, *, base: str, branch: str, item_id: str, title: str) -> GitOutcome:
    """The framework-owned tail of a managed run. Every step degrades gracefully —
    a commit survives a failed push, a push survives a failed PR create — and errors
    accumulate in the result instead of raising."""
    out = GitOutcome(branch=branch, item_id=item_id, base=base)
    env = await _identity_env(workdir)

    # Isolation guard: never commit on the base branch. A distinct signal, not just a
    # refusal — the caller must surface this as NOT done (protoMaker's strandedOnBase).
    head = await _git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
    head_branch = head.stdout.strip() if head.ok else ""
    if head_branch == base:
        out.stranded_on_base = True
        out.errors.append(f"work left uncommitted in {workdir} — recover or re-run")
        return out
    if head_branch == "HEAD" or not head_branch:
        # Detached HEAD (a crashed rebase/bisect, or the coder checked out a sha) —
        # `--abbrev-ref` returns the literal string "HEAD". Publishing would push a
        # branch named "HEAD"; refuse and leave the work recoverable instead.
        out.blocked_reason = "the worktree is in detached-HEAD state — nothing was committed or pushed."
        out.errors.append(f"work (if any) left in {workdir} — reattach the branch and re-run")
        return out
    if head_branch and head_branch != branch:
        # The coder moved HEAD to its own branch despite the directive. Publishing what
        # it committed beats publishing an empty ref — adopt its branch, note the drift.
        out.errors.append(f"coder moved HEAD to {head_branch!r}; publishing that branch instead of {branch!r}")
        out.branch = branch = head_branch

    # Stage everything; exclusion is structural (.gitignore + info/exclude), plus a
    # belt-and-suspenders sweep for force-added scratch.
    add = await _git(workdir, "add", "-A", timeout=120)
    if not add.ok:
        out.errors.append(f"git add failed: {(add.stderr or add.error or '?')[:200]}")
    staged = await _git(workdir, "diff", "--cached", "--name-only")
    if staged.ok:
        scratch = [p for p in staged.stdout.splitlines() if any(p.startswith(d) for d in _SCRATCH_DIRS)]
        for path in scratch:
            await _git(workdir, "reset", "-q", "HEAD", "--", path)
        if scratch:
            out.errors.append(f"unstaged scratch paths: {', '.join(scratch[:5])}{'…' if len(scratch) > 5 else ''}")

    # Secret scan on what would be committed — the harness commit is the one reliable
    # interception point. The same diff text answers "is anything staged?" (its
    # emptiness), so no separate `--quiet` probe.
    diff = await _git(workdir, "diff", "--cached", timeout=120)
    if diff.ok:
        found = _scan_added_lines(diff.stdout)
        if found:
            await _git(workdir, "reset", "-q")
            out.blocked_secrets = found
            return out
        have_staged = bool(diff.stdout.strip())
    else:
        # Diff unreadable — fall back to the exit-code probe. `--quiet` exits 1 for
        # "staged changes present"; any OTHER nonzero (128 = corrupt index, …) is an
        # error, not a yes.
        staged_check = await _git(workdir, "diff", "--cached", "--quiet")
        have_staged = staged_check.error is None and staged_check.returncode == 1

    if have_staged:
        commit = await _git(workdir, "commit", "--no-verify", "-m", f"{title}\n\nItem ID: {item_id}", env=env)
        if not commit.ok:
            out.errors.append(f"commit failed: {(commit.stderr or commit.error or '?')[:300]}")
            return out
        out.committed = True
    else:
        # Clean tree — three-tier probe for git the coder did itself (edit-only is an
        # instruction, not an invariant).
        await _git(workdir, "fetch", "origin", branch)  # best-effort; remote may not exist yet
        remote_exists = (await _git(workdir, "rev-parse", "--verify", "--quiet", f"origin/{branch}")).ok
        local_ahead_base = await _count(workdir, f"origin/{base}..HEAD")
        if local_ahead_base is None:
            # _count contract: None = UNKNOWN (origin/<base> unresolvable), never 0.
            # Claiming no_changes here would strand committed work behind an honest-
            # looking "nothing to publish" — refuse loudly instead.
            out.blocked_reason = f"origin/{base} is unresolvable — cannot tell whether the coder committed work."
            out.errors.append(f"fetch origin/{base} and re-run; work (if any) is intact in {workdir}")
            return out
        local_unpushed = await _count(workdir, f"origin/{branch}..HEAD") if remote_exists else None
        remote_ahead_base = (await _count(workdir, f"origin/{base}..origin/{branch}") or 0) if remote_exists else 0
        if local_ahead_base and (local_unpushed is None or local_unpushed):
            # Unknown unpushed-count ⇒ assume unpushed: re-pushing is idempotent,
            # stranding is not.
            out.coder_did_git = "committed"
            out.committed = True
        elif remote_ahead_base:
            sha = await _git(workdir, "rev-parse", f"origin/{branch}")
            if not (sha.ok and sha.stdout.strip()):
                out.blocked_reason = f"origin/{branch} is ahead of base but its sha is unresolvable."
                return out
            out.coder_did_git = "pushed"
            out.pushed, out.pushed_sha = True, sha.stdout.strip()
        else:
            out.no_changes = True
            return out

    if out.committed and not out.pushed:
        # Rebase on the freshest base, then push. A conflict is merge-time friction,
        # not a failure: abort the rebase and push what we have, reporting the files.
        lease = False
        fetched = await _git(workdir, "fetch", "origin", base, timeout=120)
        if fetched.ok:
            rebase = await _git(workdir, "rebase", f"origin/{base}", env=env, timeout=180)
            if rebase.ok:
                lease = True
            else:
                if "conflict" in f"{rebase.stdout} {rebase.stderr}".lower():
                    conflicted = await _git(workdir, "diff", "--name-only", "--diff-filter=U")
                    out.rebase_conflicts = conflicted.stdout.split() if conflicted.ok else ["(unknown)"]
                await _git(workdir, "rebase", "--abort")

        push = await _push_with_retry(workdir, branch, lease)
        head_sha = await _git(workdir, "rev-parse", "HEAD")
        out.commit_sha = head_sha.stdout.strip() if head_sha.ok else ""
        if not push.ok:
            blob = f"{push.stdout} {push.stderr}".lower()
            if "stale info" in blob or "rejected" in blob:
                out.errors.append(
                    f"push refused: the remote branch moved since our fetch (a concurrent writer?) — "
                    f"not overwriting it. Inspect origin/{branch} and re-run."
                )
            else:
                out.errors.append(f"push failed: {(push.stderr or push.error or '?')[:300]}")
            return out
        # Verify the remote actually has our HEAD — "committed locally" never counts
        # as done (the industry's #1 false-success mode).
        remote = await _git(workdir, "ls-remote", "origin", f"refs/heads/{branch}", timeout=60)
        remote_sha = remote.stdout.split()[0] if remote.ok and remote.stdout.strip() else ""
        if remote_sha and remote_sha == out.commit_sha:
            out.pushed, out.pushed_sha = True, remote_sha
        else:
            out.errors.append(f"push not verified: remote is at {remote_sha[:10] or '(unknown)'}, local at {out.commit_sha[:10]}")
            return out

    if out.pushed:
        await _open_pr(workdir, out, title=title)
    return out


async def _push_with_retry(workdir: str, branch: str, lease: bool) -> ShellResult:
    """Push with bounded backoff on infrastructure failures. ``--force-with-lease``
    only after a successful rebase (rewritten history); never bare ``--force``.

    A lease REJECTION ("stale info") is deliberately NOT retried: fetching the branch
    and re-pushing would move the lease baseline to the concurrent writer's tip and
    the retry would silently overwrite their commits — the exact accident the lease
    exists to prevent. It surfaces as a push failure for the operator to resolve."""
    args = ["push", *(["--force-with-lease"] if lease else []), "-u", "origin", branch]
    for attempt in range(2):
        res = await _git(workdir, *args, timeout=120)
        if res.ok:
            return res
        blob = f"{res.stdout} {res.stderr} {res.error or ''}".lower()
        transient = any(
            m in blob for m in ("cannot lock ref", "could not resolve host", "unable to access", "connection")
        )
        if not transient:
            return res
        await asyncio.sleep(0.5 * (2**attempt))
    return await _git(workdir, *args, timeout=120)


# ── PR layer (idempotent) ─────────────────────────────────────────────────────


async def _pr_url_for(workdir: str, branch: str, *, state: str) -> str:
    """URL of the newest PR for ``branch`` in ``state`` (gh's open/closed/merged/all),
    or ""."""
    rc, stdout, _ = await run_gh(
        ["pr", "list", "--head", branch, "--state", state, "--json", "url", "--limit", "1"],
        cwd=workdir,
    )
    if rc == 0 and stdout.strip():
        try:
            rows = json.loads(stdout)
            if rows:
                return str(rows[0].get("url", ""))
        except ValueError:
            pass
    return ""


async def _pr_url_by_item(workdir: str, item_id: str, *, state: str) -> str:
    """URL of a PR whose head branch carries this item's ``-<id7>`` suffix, or "".
    Dedup keys on the STABLE item id, not the (possibly inferred, run-to-run varying)
    slug — so a re-run that names the branch differently still finds the prior PR."""
    suffix = f"-{item_id[-7:]}"
    rc, stdout, _ = await run_gh(
        ["pr", "list", "--state", state, "--json", "url,headRefName", "--limit", "50"],
        cwd=workdir,
    )
    if rc == 0 and stdout.strip():
        try:
            for row in json.loads(stdout):
                if str(row.get("headRefName", "")).endswith(suffix):
                    return str(row.get("url", ""))
        except ValueError:
            pass
    return ""


async def preflight_pr(workdir: str, item_id: str) -> str:
    """URL of an already-open PR for this ITEM (matched by its stable ``-<id7>`` branch
    suffix), or "". Run BEFORE dispatching the coder: it catches restarts and the
    created-a-PR-but-crashed case across process lifetimes, which the in-memory claim
    registry can't — and keying on the item id (not the slug) makes it robust even when
    the slug was inferred differently on the re-run."""
    return await _pr_url_by_item(workdir, item_id, state="open")


async def _open_pr(workdir: str, out: GitOutcome, *, title: str) -> None:
    """Create-or-reuse the PR for ``out.branch``. Idempotency layers: 0-commits-ahead
    skip → open-PR pre-check → create → "already exists" recovery."""
    ahead = await _count(workdir, f"origin/{out.base}..origin/{out.branch}")
    if ahead == 0:
        out.pr_state = "skipped-no-commits"
        return
    existing = await preflight_pr(workdir, out.item_id)
    if existing:
        out.pr_url, out.pr_state = existing, "existing"
        return
    body = f"Automated change for item `{out.item_id}` (branch `{out.branch}`).\n\nPublished by the managed-git harness (ADR 0076)."
    rc, stdout, stderr = await run_gh(
        ["pr", "create", "--head", out.branch, "--base", out.base, "--title", title, "--body", body],
        cwd=workdir,
        timeout=60,
    )
    if rc == 0:
        url = next((ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("http")), stdout.strip())
        out.pr_url, out.pr_state = url, "created"
        return
    if "already exists" in stderr.lower():
        url = await _pr_url_for(workdir, out.branch, state="all")
        if url:
            out.pr_url, out.pr_state = url, "existing"
            return
    out.errors.append(f"gh pr create failed: {stderr[:300] or '(no stderr)'}")
