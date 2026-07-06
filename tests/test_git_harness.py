"""Managed-git harness (ADR 0076) — real-git lifecycle tests + claim/dedup.

Follows the repo's real-subprocess style (tests/test_shell.py): a real ``git init``
repo in tmp_path with a bare local "origin", so branch/commit/push behavior is
exercised for real. Only the ``gh`` PR layer is faked (monkeypatched ``run_gh``).
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.coding_agent import git_harness as harness
from tools.shell import run_command


async def _git(cwd, *args) -> str:
    res = await run_command(["git", *args], cwd=str(cwd))
    assert res.ok, f"git {' '.join(args)}: {res.stderr or res.error}"
    return res.stdout


@pytest.fixture
async def repo(tmp_path):
    """A work clone with a local bare origin, one pushed commit on main."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    assert (await run_command(["git", "init", "--bare", str(origin)])).ok
    assert (await run_command(["git", "clone", str(origin), str(work)])).ok
    await _git(work, "config", "user.email", "test@example.com")
    await _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("hello\n")
    await _git(work, "add", "-A")
    await _git(work, "commit", "-m", "init")
    await _git(work, "branch", "-M", "main")
    await _git(work, "push", "-u", "origin", "main")
    return work


def _fake_gh(monkeypatch, responses: dict[str, tuple[int, str, str]]) -> list[list[str]]:
    """Fake ``run_gh`` keyed on the first two args ('pr list' / 'pr create')."""
    calls: list[list[str]] = []

    async def fake(args, timeout=30, cwd=None):
        calls.append(list(args))
        return responses.get(" ".join(args[:2]), (1, "", "no fake response"))

    monkeypatch.setattr(harness, "run_gh", fake)
    return calls


# ── deterministic identity ────────────────────────────────────────────────────


def test_mint_branch_deterministic_and_unique():
    a = harness.mint_branch("proto-1", "Fix the flaky VRAM chart", "abc123def456")
    assert a == harness.mint_branch("proto-1", "Fix the flaky VRAM chart", "abc123def456")
    assert a == "proto-1/fix-the-flaky-vram-chart-3def456"
    # Same title, different item ⇒ different branch (the id suffix is the collision-proofing).
    b = harness.mint_branch("proto-1", "Fix the flaky VRAM chart", "zzz999zzz999")
    assert a != b


def test_mint_branch_survives_hostile_input():
    branch = harness.mint_branch("Proto 1!", "  Émojis 🎉 & spaces:\nsecond line ignored", "abc123def456")
    prefix, _, slug = branch.partition("/")
    assert prefix == "proto-1"
    assert slug.endswith("-3def456")
    assert " " not in branch and ":" not in branch
    # Empty everything still yields a valid ref.
    assert harness.mint_branch("", "", "abc123def456") == "task/task-3def456"


def test_derive_item_id_stable_and_whitespace_insensitive():
    assert harness.derive_item_id("do the thing") == harness.derive_item_id("  do the thing  \n")
    assert len(harness.derive_item_id("x")) == 12


def test_title_from_caps_and_collapses():
    assert harness.title_from("  Fix   the\tthing  \nrest is body") == "Fix the thing"
    assert len(harness.title_from("x" * 200)) <= 72


# ── claim registry ────────────────────────────────────────────────────────────


def test_claim_and_release():
    assert harness.claim("item-1", "coder-a") is None
    assert harness.claim("item-1", "coder-b") == "coder-a"
    harness.release("item-1")
    assert harness.claim("item-1", "coder-b") is None
    harness.release("item-1")
    harness.release("item-1")  # idempotent


async def test_claim_dedups_concurrent_fanout():
    got: list[str | None] = []

    async def worker(name: str):
        holder = harness.claim("item-fan", name)
        got.append(holder)
        if holder is None:
            await asyncio.sleep(0.02)  # hold the claim while the others arrive
            harness.release("item-fan")

    await asyncio.gather(*(worker(f"c{i}") for i in range(4)))
    assert got.count(None) == 1  # exactly one dispatch wins


# ── prepare ───────────────────────────────────────────────────────────────────


async def test_prepare_branches_off_origin_not_local_head(repo):
    # Diverge LOCAL main with an unpushed commit — the branch must not inherit it.
    (repo / "local-only.txt").write_text("x\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "local drift")
    drift_sha = await _git(repo, "rev-parse", "HEAD")

    prep = await harness.prepare(str(repo), base="main", branch="t/task-abc1234")
    assert prep.error is None
    assert await _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "t/task-abc1234"
    res = await run_command(["git", "merge-base", "--is-ancestor", drift_sha, "HEAD"], cwd=str(repo))
    assert res.returncode != 0  # the local-drift commit is NOT on the new branch


async def test_prepare_stashes_leftovers_and_seeds_exclude(repo):
    (repo / "leftover.txt").write_text("stranded work\n")
    prep = await harness.prepare(str(repo), base="main", branch="t/clean-abc1234")
    assert prep.error is None
    assert any("stashed leftover" in n for n in prep.notes)
    assert (await _git(repo, "status", "--porcelain")) == ""
    assert "leftover" in (await _git(repo, "stash", "list"))
    exclude = await _git(repo, "rev-parse", "--git-path", "info/exclude")
    path = repo / exclude if not exclude.startswith("/") else exclude
    assert ".proto/" in open(path).read()


async def test_prepare_keeps_branch_with_prior_commits(repo):
    await harness.prepare(str(repo), base="main", branch="t/keep-abc1234")
    (repo / "work.txt").write_text("prior run\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "prior partial run")
    sha = await _git(repo, "rev-parse", "HEAD")
    await _git(repo, "checkout", "main")

    prep = await harness.prepare(str(repo), base="main", branch="t/keep-abc1234")
    assert prep.error is None
    assert await _git(repo, "rev-parse", "HEAD") == sha  # not re-cut — prior work kept


# ── finish ────────────────────────────────────────────────────────────────────


async def test_finish_commits_pushes_and_opens_pr(repo, monkeypatch):
    calls = _fake_gh(
        monkeypatch,
        {"pr list": (0, "[]", ""), "pr create": (0, "https://github.com/x/y/pull/7", "")},
    )
    await harness.prepare(str(repo), base="main", branch="t/feat-abc1234")
    (repo / "feature.txt").write_text("new\n")
    (repo / ".proto").mkdir()
    (repo / ".proto" / "scratch.md").write_text("coder scratch\n")  # excluded structurally

    out = await harness.finish(str(repo), base="main", branch="t/feat-abc1234", item_id="abc1234", title="Add feature")
    assert out.committed and out.pushed
    assert out.pushed_sha == out.commit_sha
    assert out.pr_url.endswith("/pull/7") and out.pr_state == "created"
    # Remote really has the commit, and scratch never reached it.
    remote_sha = (await _git(repo, "ls-remote", "origin", "refs/heads/t/feat-abc1234")).split()[0]
    assert remote_sha == out.commit_sha
    files = await _git(repo, "diff", "--name-only", "origin/main..HEAD")
    assert "feature.txt" in files and ".proto/scratch.md" not in files
    assert any(a[:2] == ["pr", "create"] for a in calls)
    assert "Item ID: abc1234" in (await _git(repo, "log", "-1", "--format=%B"))


async def test_finish_no_changes_is_honest(repo, monkeypatch):
    _fake_gh(monkeypatch, {"pr list": (0, "[]", "")})
    await harness.prepare(str(repo), base="main", branch="t/noop-abc1234")
    out = await harness.finish(str(repo), base="main", branch="t/noop-abc1234", item_id="abc1234", title="Nothing")
    assert out.no_changes and not out.committed and not out.pushed and not out.pr_url
    assert "no changes" in out.render()


async def test_finish_adopts_coder_commit(repo, monkeypatch):
    _fake_gh(monkeypatch, {"pr list": (0, "[]", ""), "pr create": (0, "https://github.com/x/y/pull/8", "")})
    await harness.prepare(str(repo), base="main", branch="t/adopt-abc1234")
    (repo / "coder.txt").write_text("coder did git itself\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "coder's own commit")

    out = await harness.finish(str(repo), base="main", branch="t/adopt-abc1234", item_id="abc1234", title="Adopt")
    assert out.coder_did_git == "committed"
    assert out.pushed and out.pr_url.endswith("/pull/8")


async def test_finish_stranded_on_base_refuses(repo, monkeypatch):
    _fake_gh(monkeypatch, {"pr list": (0, "[]", "")})
    await harness.prepare(str(repo), base="main", branch="t/stray-abc1234")
    await _git(repo, "checkout", "main")
    (repo / "oops.txt").write_text("edited on main\n")

    out = await harness.finish(str(repo), base="main", branch="t/stray-abc1234", item_id="abc1234", title="Stray")
    assert out.stranded_on_base and not out.committed and not out.pushed
    assert "BLOCKED" in out.render()
    # Nothing was committed to main; the work is still there, uncommitted.
    assert (await _git(repo, "rev-list", "--count", "origin/main..main")) == "0"
    assert "oops.txt" in (await _git(repo, "status", "--porcelain"))


async def test_finish_blocks_secrets(repo, monkeypatch):
    _fake_gh(monkeypatch, {"pr list": (0, "[]", "")})
    await harness.prepare(str(repo), base="main", branch="t/leak-abc1234")
    (repo / "config.py").write_text('AWS_KEY = "AKIA' + "ABCDEFGHIJKLMNOP" + '"\n')

    out = await harness.finish(str(repo), base="main", branch="t/leak-abc1234", item_id="abc1234", title="Leak")
    assert out.blocked_secrets and not out.committed and not out.pushed
    assert "config.py" in out.blocked_secrets[0]
    assert (await _git(repo, "rev-list", "--count", "origin/main..HEAD")) == "0"


async def test_finish_detached_head_refuses(repo, monkeypatch):
    _fake_gh(monkeypatch, {"pr list": (0, "[]", "")})
    await harness.prepare(str(repo), base="main", branch="t/detach-abc1234")
    await _git(repo, "checkout", "--detach")
    (repo / "adrift.txt").write_text("edited while detached\n")

    out = await harness.finish(str(repo), base="main", branch="t/detach-abc1234", item_id="abc1234", title="Adrift")
    assert out.blocked_reason and "detached" in out.blocked_reason
    assert not out.committed and not out.pushed
    assert "BLOCKED" in out.render()
    # No branch literally named HEAD was minted or pushed.
    res = await run_command(["git", "rev-parse", "--verify", "refs/heads/HEAD"], cwd=str(repo))
    assert res.returncode != 0


async def test_finish_unresolvable_base_blocks_instead_of_no_changes(repo, monkeypatch):
    """_count contract: None = unknown, never 0 — committed work must not be
    reported as 'no changes to publish'."""
    _fake_gh(monkeypatch, {"pr list": (0, "[]", "")})
    await harness.prepare(str(repo), base="main", branch="t/ghost-abc1234")
    (repo / "real-work.txt").write_text("committed by the coder\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "coder work")

    out = await harness.finish(str(repo), base="ghost", branch="t/ghost-abc1234", item_id="abc1234", title="Ghost")
    assert not out.no_changes
    assert out.blocked_reason and "unresolvable" in out.blocked_reason
    assert not out.pushed


async def test_push_lease_rejection_never_clobbers_concurrent_writer(repo, tmp_path, monkeypatch):
    """A lease rejection must surface as a failure — never fetch-and-force over a
    concurrent writer's commits."""
    _fake_gh(monkeypatch, {"pr list": (0, "[]", "")})
    branch = "t/race-abc1234"
    await harness.prepare(str(repo), base="main", branch=branch)
    (repo / "ours.txt").write_text("our work\n")

    # A concurrent writer pushes to the same branch first, from a second clone.
    other = tmp_path / "other"
    origin_url = (await _git(repo, "remote", "get-url", "origin")).strip()
    assert (await run_command(["git", "clone", origin_url, str(other)])).ok
    await _git(other, "config", "user.email", "rival@example.com")
    await _git(other, "config", "user.name", "Rival")
    await _git(other, "checkout", "-b", branch, "origin/main")
    (other / "theirs.txt").write_text("their work\n")
    await _git(other, "add", "-A")
    await _git(other, "commit", "-m", "concurrent work")
    await _git(other, "push", "-u", "origin", branch)
    their_sha = await _git(other, "rev-parse", "HEAD")

    out = await harness.finish(str(repo), base="main", branch=branch, item_id="abc1234", title="Race")
    assert not out.pushed
    assert any("refused" in e or "failed" in e for e in out.errors)
    # The concurrent writer's commit survived on the remote.
    remote_sha = (await _git(repo, "ls-remote", "origin", f"refs/heads/{branch}")).split()[0]
    assert remote_sha == their_sha


async def test_registry_raw_dispatch_bypasses_managed_git(repo, monkeypatch):
    """The coder ladder (ADR 0064) consumes replies as candidate code — raw=True must
    skip branch/commit/PR and the claim."""
    from plugins.delegates.adapters import AcpAdapter
    from plugins.delegates.registry import DelegateRegistry

    async def fake_prompt(self, d, query, *, timeout=None):
        return "```python\nprint('candidate')\n```"

    monkeypatch.setattr(AcpAdapter, "_prompt", fake_prompt)
    reg = DelegateRegistry(
        [{"name": "c1", "type": "acp", "command": "fake", "workdir": str(repo), "manage_git": "true"}]
    )
    reply = await reg.dispatch("c1", "same prompt", raw=True)
    assert "candidate" in reply and "[managed git]" not in reply
    assert await _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # no branch minted
    # And a second identical raw dispatch is NOT deduped away (best-of-k stays k).
    reply2 = await reg.dispatch("c1", "same prompt", raw=True)
    assert "candidate" in reply2 and "already being built" not in reply2


async def test_finish_reuses_existing_pr(repo, monkeypatch):
    _fake_gh(monkeypatch, {"pr list": (0, '[{"url": "https://github.com/x/y/pull/3"}]', "")})
    await harness.prepare(str(repo), base="main", branch="t/reuse-abc1234")
    (repo / "again.txt").write_text("re-run\n")

    out = await harness.finish(str(repo), base="main", branch="t/reuse-abc1234", item_id="abc1234", title="Re-run")
    assert out.pushed
    assert out.pr_url.endswith("/pull/3") and out.pr_state == "existing"


# ── managed dispatch (adapter integration) ────────────────────────────────────


def _managed_delegate(repo, name="coder-1"):
    from plugins.delegates.adapters import Delegate

    return Delegate(name=name, type="acp", command="fake", workdir=str(repo), manage_git=True, base_branch="main")


async def test_managed_dispatch_end_to_end(repo, monkeypatch):
    from plugins.delegates.adapters import AcpAdapter

    _fake_gh(monkeypatch, {"pr list": (0, "[]", ""), "pr create": (0, "https://github.com/x/y/pull/9", "")})
    seen_queries: list[str] = []

    async def fake_prompt(self, d, query, *, timeout=None):
        seen_queries.append(query)
        (repo / "built.txt").write_text("done\n")
        return "I made the change and ran the tests."

    monkeypatch.setattr(AcpAdapter, "_prompt", fake_prompt)
    reply = await AcpAdapter().dispatch(_managed_delegate(repo), "Add the built file", item_id="item-42")

    assert "I made the change" in reply
    assert "pull/9" in reply and "[managed git]" in reply
    assert "Do NOT run git commands" in seen_queries[0]  # edit-only directive reached the coder
    branch = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch == "coder-1/add-the-built-file-item-42"
    assert harness.claim("item-42", "probe") is None  # claim was released
    harness.release("item-42")


async def test_managed_dispatch_dedups_inflight_item(repo, monkeypatch):
    from plugins.delegates.adapters import AcpAdapter

    _fake_gh(monkeypatch, {"pr list": (0, "[]", ""), "pr create": (0, "https://github.com/x/y/pull/10", "")})

    async def slow_prompt(self, d, query, *, timeout=None):
        (repo / "slow.txt").write_text("done\n")
        await asyncio.sleep(0.05)
        return "built it"

    monkeypatch.setattr(AcpAdapter, "_prompt", slow_prompt)
    adapter = AcpAdapter()
    task = "One item fanned to two coders"
    a, b = await asyncio.gather(
        adapter.dispatch(_managed_delegate(repo, "coder-1"), task),
        adapter.dispatch(_managed_delegate(repo, "coder-2"), task),  # same task ⇒ same derived item_id
    )
    replies = sorted([a, b])
    assert sum("already being built" in r for r in replies) == 1
    assert sum("[managed git]" in r for r in replies) == 1


async def test_managed_dispatch_preflight_returns_existing_pr(repo, monkeypatch):
    from plugins.delegates.adapters import AcpAdapter

    _fake_gh(monkeypatch, {"pr list": (0, '[{"url": "https://github.com/x/y/pull/11"}]', "")})

    async def must_not_run(self, d, query, *, timeout=None):
        raise AssertionError("coder must not be dispatched when an open PR exists")

    monkeypatch.setattr(AcpAdapter, "_prompt", must_not_run)
    reply = await AcpAdapter().dispatch(_managed_delegate(repo), "Anything", item_id="item-43")
    assert "pull/11" in reply and "already exists" in reply


async def test_unmanaged_dispatch_untouched(repo, monkeypatch):
    """manage_git=False keeps the old path — no branch, no git, no directive."""
    from plugins.delegates.adapters import AcpAdapter, Delegate

    async def fake_prompt(self, d, query, *, timeout=None):
        return f"plain: {query}"

    monkeypatch.setattr(AcpAdapter, "_prompt", fake_prompt)
    d = Delegate(name="c", type="acp", command="fake", workdir=str(repo))
    reply = await AcpAdapter().dispatch(d, "just a task")
    assert reply == "plain: just a task"
    assert await _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
