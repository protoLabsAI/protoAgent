"""The ``onboard_project`` tool and its factory (#2555).

Covers the disposition axis (present only when ``onboarding.enabled``) and the
tool end-to-end with git + the config writer mocked out — the point is the
BOUNDS, so most of these are refusal paths:

  - disabled  → the factory yields no tool at all
  - enabled   → exactly one tool named ``onboard_project``
  - outside ``allow``  → refused, naming the pattern set; nothing cloned/registered
  - outside ``root``   → refused, naming the root; nothing cloned/registered
  - happy path → subprocess git clone (no shell=True) + a merged registration
  - existing checkout → reused as-is (no re-clone), still registered
  - clone failure → the git stderr is surfaced
  - reuse drift (#3402) → local ahead/behind vs the tracking branch is reported
    (or a bounded not-comparable note), and NO clone/fetch/reset/checkout runs

git and the ``HOST.apply_settings`` seam are mocked, so no real clone or config
write happens; ``tmp_path`` is the onboarding root so the directory checks are real.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from graph.config import LangGraphConfig
from graph.plugins.host import HOST
from tools import onboard_tools


def _cfg(tmp_path: Path, **over) -> LangGraphConfig:
    """A LangGraphConfig with onboarding enabled and pointed at ``tmp_path``."""
    kw = dict(
        onboarding_enabled=True,
        onboarding_root=str(tmp_path),
        onboarding_allow=["github.com/acme/*"],
        onboarding_write_default=False,
    )
    kw.update(over)
    return LangGraphConfig(**kw)


def _tool(config: LangGraphConfig):
    tools = onboard_tools.build_onboard_tools(config)
    assert len(tools) == 1
    return tools[0]


class _Mocks:
    """Records git clone + config-apply calls without performing either.

    The read-only probes (``git symbolic-ref`` for the default branch, and the
    ``git rev-parse``/``git rev-list`` tracking-drift probes of #3402) are answered
    from configurable canned values and recorded as probes — never as clones. Every
    git argv is also captured in ``git_calls`` so a test can prove that no clone,
    fetch, reset, or checkout-mutating command was ever issued on a reuse path.
    """

    def __init__(
        self,
        returncode: int = 0,
        stderr: str = "",
        *,
        upstream: str = "origin/main",
        upstream_rc: int = 0,
        upstream_stderr: str = "",
        counts: str = "0\t0",
        counts_rc: int = 0,
    ) -> None:
        self.clone_calls: list[tuple[tuple, dict]] = []
        self.probe_calls: list[tuple[tuple, dict]] = []
        self.apply_calls: list[dict] = []
        self.git_calls: list[list[str]] = []
        self._returncode = returncode
        self._stderr = stderr
        self._upstream = upstream
        self._upstream_rc = upstream_rc
        self._upstream_stderr = upstream_stderr
        self._counts = counts
        self._counts_rc = counts_rc

    def fake_run(self, *args, **kwargs):
        argv = list(args[0] if args else kwargs.get("args") or [])
        self.git_calls.append(argv)
        head = argv[:2]
        if head == ["git", "symbolic-ref"]:
            # the default-branch probe (origin/HEAD) — answered, never counted as a clone
            self.probe_calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="origin/develop\n", stderr="")
        if head == ["git", "rev-parse"]:
            # the tracking-branch resolution (@{upstream}) — read-only
            self.probe_calls.append((args, kwargs))
            stdout = f"{self._upstream}\n" if self._upstream_rc == 0 else ""
            return SimpleNamespace(returncode=self._upstream_rc, stdout=stdout, stderr=self._upstream_stderr)
        if head == ["git", "rev-list"]:
            # the ahead/behind count against @{upstream} — read-only
            self.probe_calls.append((args, kwargs))
            stdout = f"{self._counts}\n" if self._counts_rc == 0 else ""
            return SimpleNamespace(returncode=self._counts_rc, stdout=stdout, stderr="")
        self.clone_calls.append((args, kwargs))
        return SimpleNamespace(returncode=self._returncode, stdout="", stderr=self._stderr)

    def fake_apply(self, patch):
        self.apply_calls.append(patch)
        return True, ["config saved"]


@pytest.fixture
def mocks(monkeypatch):
    """Patch git and the HOST.apply_settings seam; yield the recorder."""
    m = _Mocks()
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)
    return m


# ---------------------------------------------------------------------------
# factory disposition
# ---------------------------------------------------------------------------


def test_disabled_returns_empty_tools(tmp_path):
    config = _cfg(tmp_path, onboarding_enabled=False)
    assert onboard_tools.build_onboard_tools(config) == []


def test_enabled_returns_tool(tmp_path):
    tools = onboard_tools.build_onboard_tools(_cfg(tmp_path))
    assert [t.name for t in tools] == ["onboard_project"]


# ---------------------------------------------------------------------------
# the stock default (#3396)
# ---------------------------------------------------------------------------
#
# `onboarding.enabled` defaults ON so the surface is discoverable — the tool exists
# and can NAME what is missing, and the settings fields stop hiding. These pin the
# claim that pays for that: the switch is not the consent, the bounds are, and a
# stock install can still onboard exactly nothing.


def test_default_config_surfaces_the_tool():
    assert [t.name for t in onboard_tools.build_onboard_tools(LangGraphConfig())] == ["onboard_project"]


@pytest.mark.asyncio
async def test_default_config_refuses_every_source(mocks):
    """Empty `allow` matches nothing — so a stock install clones nothing, and the
    refusal names the (empty) pattern set rather than failing silently."""
    out = await _tool(LangGraphConfig()).ainvoke({"github_repo": "acme/widgets"})
    assert out.startswith("Refused:")
    # Names the remedy, not just the state: being unconfigured is the NORMAL stock
    # condition now that `enabled` defaults on, so the refusal has to teach.
    assert "no allowed clone sources are configured" in out
    assert "Allowed sources" in out
    assert mocks.clone_calls == [] and mocks.apply_calls == []


@pytest.mark.asyncio
async def test_default_config_has_no_space_to_clone_into(mocks):
    """Even with a source allowed, an unset root leaves nowhere consented to write."""
    config = LangGraphConfig(onboarding_allow=["github.com/acme/*"])
    assert config.onboarding_root == ""
    out = await _tool(config).ainvoke({"github_repo": "acme/widgets"})
    assert out.startswith(("Refused:", "Error:"))
    assert mocks.clone_calls == [] and mocks.apply_calls == []


# ---------------------------------------------------------------------------
# refusal paths
# ---------------------------------------------------------------------------


async def test_refuse_outside_allow(tmp_path, mocks):
    tool = _tool(_cfg(tmp_path, onboarding_allow=["github.com/acme/*"]))
    out = await tool.ainvoke({"github_repo": "evil/repo"})

    assert out.startswith("Refused:")
    assert "github.com/acme/*" in out  # names the bound it didn't match
    assert "github.com/evil/repo" in out
    assert mocks.clone_calls == []  # nothing cloned
    assert mocks.apply_calls == []  # nothing registered


async def test_refuse_outside_root(tmp_path, mocks):
    # The allow glob matches (fnmatch '*' spans slashes) so we reach the root check;
    # the '../' traversal then resolves above the onboarding root.
    tool = _tool(_cfg(tmp_path, onboarding_allow=["github.com/acme/*"]))
    out = await tool.ainvoke({"github_repo": "acme/../../../../etc/evil"})

    assert out.startswith("Refused:")
    assert "onboarding root" in out
    assert str(tmp_path) in out  # names the root bound
    assert mocks.clone_calls == []  # nothing cloned
    assert mocks.apply_calls == []  # nothing registered


# ---------------------------------------------------------------------------
# happy path + reuse + failure
# ---------------------------------------------------------------------------


async def test_happy_path_clone_and_register(tmp_path, mocks):
    tool = _tool(_cfg(tmp_path))
    out = await tool.ainvoke({"github_repo": "https://github.com/acme/widget"})

    target = tmp_path / "widget"

    # git clone: exactly the expected argv, off the event loop, and NEVER shell=True.
    assert len(mocks.clone_calls) == 1
    args, kwargs = mocks.clone_calls[0]
    assert args[0] == ["git", "clone", "https://github.com/acme/widget.git", str(target)]
    assert kwargs.get("shell") is not True
    assert "shell" not in kwargs
    assert kwargs.get("timeout") == 120
    assert kwargs.get("capture_output") is True

    # registration: the ADR 0095 `projects:` REGISTRY carries the new entry — with its
    # github binding + default branch (probed from origin/HEAD) — because that is what
    # the fs fence, the github plugin's picker and the board all project from. No
    # explicit filesystem.projects override exists here, so none is written: writing
    # one would SHADOW the registry for every future entry (D2: explicit wins).
    assert len(mocks.apply_calls) == 1
    applied = mocks.apply_calls[0]
    assert {
        "name": "widget",
        "path": str(target),
        "github": "acme/widget",
        "default_branch": "develop",
        "write": False,
    } in applied["projects"]
    assert applied["filesystem"] == {"enabled": True}
    assert len(mocks.probe_calls) == 1 and mocks.probe_calls[0][1].get("cwd") == str(target)

    assert "widget" in out and "read-only" in out and str(target) in out
    assert "acme/widget" in out and "develop" in out and "GitHub plugin" in out


async def test_happy_path_merges_with_existing_registry(tmp_path, mocks):
    prior = {"name": "keep", "path": str(tmp_path / "keep"), "write": True, "github": "acme/keep"}
    tool = _tool(_cfg(tmp_path, projects=[prior]))
    await tool.ainvoke({"github_repo": "acme/widget"})

    projects = mocks.apply_calls[0]["projects"]
    # superset invariant: the pre-existing registration survives the merge.
    assert prior in projects
    assert any(p["path"] == str(tmp_path / "widget") for p in projects)


async def test_explicit_fence_override_is_mirrored_so_the_fs_tools_see_it(tmp_path, mocks):
    """An instance still carrying an explicit ``filesystem.projects`` (the legacy,
    pre-registry shape) shadows the registry in the fence — D2, explicit wins. The
    registry entry alone would register a repo the fs tools can't reach, so the fence
    projection is appended to the override as well. Both lists keep their priors."""
    prior = {"name": "keep", "path": str(tmp_path / "keep"), "write": True}
    tool = _tool(_cfg(tmp_path, filesystem_projects=[prior]))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    applied = mocks.apply_calls[0]
    target = str(tmp_path / "widget")
    assert any(p["path"] == target and p["github"] == "acme/widget" for p in applied["projects"])
    fence = applied["filesystem"]["projects"]
    assert prior in fence
    assert {"name": "widget", "path": target, "write": False, "github": "acme/widget"} in fence
    assert applied["filesystem"]["enabled"] is True
    assert "explicit filesystem.projects override" in out


async def test_fence_only_entry_is_promoted_into_the_registry(tmp_path, mocks):
    """A repo that exists ONLY in the legacy fence override (hand-registered before
    the registry, or by the pre-#2925 tool) is promoted into the registry on
    re-onboard — that's how it becomes visible to the github plugin — without
    touching the override it is already in."""
    target = tmp_path / "widget"
    target.mkdir()
    tool = _tool(_cfg(tmp_path, filesystem_projects=[{"name": "widget", "path": str(target), "write": False}]))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert mocks.clone_calls == []
    applied = mocks.apply_calls[0]
    assert any(p["path"] == str(target) and p["github"] == "acme/widget" for p in applied["projects"])
    assert "filesystem" not in applied  # already in the override; nothing to mirror
    assert "Promoted" in out


async def test_reuse_existing_checkout(tmp_path, mocks):
    target = tmp_path / "widget"
    target.mkdir()  # a checkout is already on disk

    tool = _tool(_cfg(tmp_path))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert mocks.clone_calls == []  # no re-clone, no fetch/reset
    assert len(mocks.apply_calls) == 1  # but registration still proceeds
    assert "Reused existing checkout" in out
    # #3402: the reuse result now names the tracking drift, read from local Git
    # metadata only, and always states the checkout was not fetched.
    assert "it was not fetched" in out
    assert "origin/main" in out


async def test_already_registered_is_idempotent(tmp_path, mocks):
    target = tmp_path / "widget"
    target.mkdir()
    entry = {"name": "widget", "path": str(target), "write": False, "github": "acme/widget"}
    tool = _tool(_cfg(tmp_path, projects=[entry]))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert mocks.clone_calls == []
    assert mocks.apply_calls == []  # nothing to write; already registered
    assert "already registered" in out


async def test_already_registered_in_both_registry_and_override_is_idempotent(tmp_path, mocks):
    target = tmp_path / "widget"
    target.mkdir()
    reg = {"name": "widget", "path": str(target), "write": False, "github": "acme/widget"}
    fence = {"name": "widget", "path": str(target), "write": False}
    tool = _tool(_cfg(tmp_path, projects=[reg], filesystem_projects=[fence]))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert mocks.apply_calls == []
    assert "already registered" in out


# ---------------------------------------------------------------------------
# tracking-branch drift on reuse (#3402)
# ---------------------------------------------------------------------------
#
# On every reuse path we inspect ONLY local Git metadata against the checkout's
# configured upstream and report ahead/behind divergence — never fetching,
# resetting, or otherwise mutating the checkout. When the drift can't be told
# (no upstream, not a git checkout, git unavailable, unparseable count) the
# result says so instead of inventing a count. Registration is unchanged.


def _mutating_git(argv: list[str]) -> bool:
    """True if a git argv would clone/fetch/reset or mutate the working tree."""
    if not argv or argv[0] != "git":
        return False
    return any(sub in argv[1:] for sub in ("clone", "fetch", "pull", "reset", "checkout", "merge", "rebase"))


async def test_reuse_reports_behind_count_and_tracking_branch(tmp_path, monkeypatch):
    """r1: behind its tracking ref → the success output names the exact behind
    count and tracking branch and explicitly says it was not fetched."""
    m = _Mocks(upstream="origin/main", counts="2\t0")  # 2 behind, 0 ahead
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "2 commits behind origin/main" in out
    assert "it was not fetched" in out
    assert m.clone_calls == []
    assert len(m.apply_calls) == 1  # registration semantics unchanged


async def test_reuse_reports_ahead_and_diverged(tmp_path, monkeypatch):
    """r2: ahead / diverged → the applicable local divergence is reported, and no
    Git state is mutated."""
    m = _Mocks(upstream="origin/main", counts="3\t1")  # 3 behind, 1 ahead → diverged
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "1 commit ahead of and 3 commits behind origin/main" in out
    assert "it was not fetched" in out
    assert not any(_mutating_git(c) for c in m.git_calls)


async def test_reuse_reports_ahead_only(tmp_path, monkeypatch):
    m = _Mocks(upstream="origin/trunk", counts="0\t5")  # 0 behind, 5 ahead
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "5 commits ahead of origin/trunk" in out
    assert "behind" not in out  # ahead-only: nothing behind to report
    assert "it was not fetched" in out


async def test_reuse_up_to_date_is_reported(tmp_path, monkeypatch):
    m = _Mocks(upstream="origin/main", counts="0\t0")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "up to date with origin/main" in out
    assert "it was not fetched" in out


async def test_reuse_no_upstream_reports_not_comparable(tmp_path, monkeypatch):
    """r3: a branch with no upstream → a bounded not-checked note, and registration
    still proceeds normally (no invented count)."""
    m = _Mocks(upstream_rc=128, upstream_stderr="fatal: no upstream configured for branch 'main'")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "no upstream" in out
    assert "it was not fetched" in out
    assert len(m.apply_calls) == 1  # registration is unchanged


async def test_reuse_non_git_dir_reports_not_comparable(tmp_path, monkeypatch):
    """r3: a reused directory that isn't a git checkout → a bounded not-determined
    note rather than a crash or a fabricated count."""
    m = _Mocks(upstream_rc=128, upstream_stderr="fatal: not a git repository (or any parent up to /)")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "not a git checkout" in out
    assert "could not be determined" in out
    assert len(m.apply_calls) == 1


async def test_reuse_unparseable_count_reports_not_comparable(tmp_path, monkeypatch):
    """r3: rev-list SUCCEEDS but its output does not parse as two counts → drift is
    reported as not-determined, naming no count.

    Split from the nonzero-exit case below, which is a different branch: this one
    reaches the parse, that one never gets there. The test used to carry this name
    while passing `counts_rc=128`, so the parse fallback was named but never executed.
    """
    m = _Mocks(upstream="origin/main", counts="not-a-count")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "could not be determined" in out
    assert "origin/main" in out
    assert len(m.apply_calls) == 1


async def test_reuse_failed_count_command_reports_not_comparable(tmp_path, monkeypatch):
    """r3: rev-list EXITS NONZERO (e.g. the upstream ref vanished between probes) →
    the same not-determined report, reached by a different path than the parse
    fallback above. Both branches converge on one message, so only separate inputs
    can prove both are wired."""
    m = _Mocks(upstream="origin/main", counts_rc=128)
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "could not be determined" in out
    assert "origin/main" in out
    assert len(m.apply_calls) == 1


async def test_reuse_git_unavailable_reports_not_comparable(tmp_path, monkeypatch):
    """r3: git can't even be spawned (e.g. not installed) → a bounded note; the
    reuse + registration still complete without raising into the turn."""

    def _raise(*args, **kwargs):
        raise FileNotFoundError("git: command not found")

    apply_calls: list[dict] = []
    monkeypatch.setattr(onboard_tools.subprocess, "run", _raise)
    monkeypatch.setattr(HOST, "apply_settings", lambda patch: (apply_calls.append(patch), (True, ["ok"]))[1])

    target = tmp_path / "widget"
    target.mkdir()
    out = await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert "git was unavailable" in out
    assert "could not be determined" in out
    assert len(apply_calls) == 1  # registration is unchanged


async def test_idempotent_path_also_reports_drift(tmp_path, monkeypatch):
    """r1: drift is reported even on the already-registered idempotent path (where
    feasible) — the checkout is on disk, so the local comparison is made and named
    without writing any config."""
    m = _Mocks(upstream="origin/main", counts="4\t0")  # 4 behind
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    entry = {"name": "widget", "path": str(target), "write": False, "github": "acme/widget"}
    out = await _tool(_cfg(tmp_path, projects=[entry])).ainvoke({"github_repo": "acme/widget"})

    assert "already registered" in out  # idempotency preserved
    assert m.apply_calls == []  # nothing written
    assert "4 commits behind origin/main" in out
    assert "it was not fetched" in out


async def test_reuse_issues_no_clone_fetch_reset_or_checkout(tmp_path, monkeypatch):
    """r4: prove that a reuse path issues ONLY read-only git commands — no clone,
    fetch, pull, reset, checkout, merge, or rebase touches the checkout."""
    m = _Mocks(upstream="origin/main", counts="1\t2")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    target = tmp_path / "widget"
    target.mkdir()
    await _tool(_cfg(tmp_path)).ainvoke({"github_repo": "acme/widget"})

    assert m.clone_calls == []
    assert m.git_calls  # some git ran (the read-only probes)
    assert not any(_mutating_git(c) for c in m.git_calls)
    # every git subcommand issued is one of the known read-only probes
    subcommands = {c[1] for c in m.git_calls if len(c) > 1}
    assert subcommands <= {"symbolic-ref", "rev-parse", "rev-list"}


# ---------------------------------------------------------------------------
# live-registry merge (#2836)
# ---------------------------------------------------------------------------


async def test_merges_against_live_config_not_build_snapshot(tmp_path, mocks, monkeypatch):
    """A second onboarding in the same turn merges against the LIVE registry:
    the build-time snapshot doesn't know about the first registration, and a
    merge against it would silently drop that project from filesystem.projects."""
    first = {"name": "widget", "path": str(tmp_path / "widget"), "write": False, "github": "acme/widget"}
    live = _cfg(tmp_path, projects=[first])
    monkeypatch.setattr(HOST, "config", lambda: live)

    tool = _tool(_cfg(tmp_path))  # build-time config: no projects registered yet
    await tool.ainvoke({"github_repo": "acme/gadget"})

    projects = mocks.apply_calls[0]["projects"]
    assert first in projects  # the mid-turn registration survives the merge
    assert any(p["path"] == str(tmp_path / "gadget") for p in projects)


async def test_raising_live_config_falls_back_and_still_registers(tmp_path, mocks, monkeypatch):
    """A raising ``HOST.config`` (a mid-reload race) must NOT crash the tool after
    a successful clone — that would strand a cloned-but-unregistered directory on
    disk. It falls back to the build-time config and finishes the registration."""

    def _boom():
        raise RuntimeError("config store mid-reload")

    monkeypatch.setattr(HOST, "config", _boom)

    prior = {"name": "keep", "path": str(tmp_path / "keep"), "write": True, "github": "acme/keep"}
    tool = _tool(_cfg(tmp_path, projects=[prior]))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert "registered" in out  # the call completed — no raise into the turn
    projects = mocks.apply_calls[0]["projects"]
    assert prior in projects  # merged against the build-time fallback
    assert any(p["path"] == str(tmp_path / "widget") for p in projects)


async def test_clone_failure_surfaces_error(tmp_path, monkeypatch):
    m = _Mocks(returncode=128, stderr="fatal: repository 'https://github.com/acme/nope.git' not found")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    tool = _tool(_cfg(tmp_path))
    out = await tool.ainvoke({"github_repo": "acme/nope"})

    assert "git clone failed" in out
    assert "repository" in out and "not found" in out
    assert m.apply_calls == []  # a failed clone never reaches registration
