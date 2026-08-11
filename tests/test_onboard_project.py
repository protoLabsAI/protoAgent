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
    """Records git clone + config-apply calls without performing either."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.clone_calls: list[tuple[tuple, dict]] = []
        self.apply_calls: list[dict] = []
        self._returncode = returncode
        self._stderr = stderr

    def fake_run(self, *args, **kwargs):
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

    # registration: a merged filesystem.projects carrying the new entry.
    assert len(mocks.apply_calls) == 1
    applied = mocks.apply_calls[0]
    projects = applied["filesystem"]["projects"]
    assert applied["filesystem"]["enabled"] is True
    assert {
        "name": "widget",
        "path": str(target),
        "write": False,
        "github": "acme/widget",
    } in projects

    assert "widget" in out and "read-only" in out and str(target) in out


async def test_happy_path_merges_with_existing_projects(tmp_path, mocks):
    prior = {"name": "keep", "path": str(tmp_path / "keep"), "write": True}
    tool = _tool(_cfg(tmp_path, filesystem_projects=[prior]))
    await tool.ainvoke({"github_repo": "acme/widget"})

    projects = mocks.apply_calls[0]["filesystem"]["projects"]
    # superset invariant: the pre-existing project survives the merge.
    assert prior in projects
    assert any(p["path"] == str(tmp_path / "widget") for p in projects)


async def test_reuse_existing_checkout(tmp_path, mocks):
    target = tmp_path / "widget"
    target.mkdir()  # a checkout is already on disk

    tool = _tool(_cfg(tmp_path))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert mocks.clone_calls == []  # no re-clone, no fetch/reset
    assert len(mocks.apply_calls) == 1  # but registration still proceeds
    assert "Reused existing checkout" in out


async def test_already_registered_is_idempotent(tmp_path, mocks):
    target = tmp_path / "widget"
    target.mkdir()
    tool = _tool(_cfg(tmp_path, filesystem_projects=[{"name": "widget", "path": str(target), "write": False}]))
    out = await tool.ainvoke({"github_repo": "acme/widget"})

    assert mocks.clone_calls == []
    assert mocks.apply_calls == []  # nothing to write; already registered
    assert "already registered" in out


async def test_clone_failure_surfaces_error(tmp_path, monkeypatch):
    m = _Mocks(returncode=128, stderr="fatal: repository 'https://github.com/acme/nope.git' not found")
    monkeypatch.setattr(onboard_tools.subprocess, "run", m.fake_run)
    monkeypatch.setattr(HOST, "apply_settings", m.fake_apply)

    tool = _tool(_cfg(tmp_path))
    out = await tool.ainvoke({"github_repo": "acme/nope"})

    assert "git clone failed" in out
    assert "repository" in out and "not found" in out
    assert m.apply_calls == []  # a failed clone never reaches registration
