"""Changing an ALREADY-registered project's access from a tool call (projectBoard-plugin#783).

``write=true`` on a read-only project parks on the "Make this project read-write?" card
and only the answer bound to the folder's realpath writes; ``write=false`` on a read-write
one lowers it with no card; ``write`` omitted or equal keeps the old replies. Everything
goes through ``set_project_write``, which may change the ``write`` key and nothing else.

Real temp git repos; only ``HOST.apply_settings`` is faked and ``interrupt`` is stubbed.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import langgraph.types
import pytest

from graph.config import LangGraphConfig
from graph.middleware.request_context import request_metadata_scope
from graph.plugins.host import HOST
from tools import onboard_tools

pytestmark = pytest.mark.platform_sensitive

ALLOW, KEEP = "Allow read-write", "Keep read-only"


def _repo(path: Path, origin: str = "git@github.com:acme/munda.git") -> Path:
    path.mkdir(parents=True)
    for argv in (["init", "-q", "-b", "trunk"], ["remote", "add", "origin", origin]):
        subprocess.run(["git", *argv], cwd=str(path), check=True, capture_output=True)
    return path.resolve()


def _entry(path: Path, *, write: bool = False, **extra) -> dict:
    return {"name": "munda", "path": str(path), "github": "acme/munda", "default_branch": "trunk", "write": write, **extra}


def _cfg(root: Path, **over) -> LangGraphConfig:
    kw = dict(onboarding_enabled=True, onboarding_root=str(root), onboarding_allow=["github.com/acme/*"])
    return LangGraphConfig(**{**kw, **over})


class _Host:
    """Fake config writer that also serves the result back as the LIVE config, so a
    second call in the same test sees what the first one wrote."""

    def __init__(self, monkeypatch):
        self.patches: list[dict] = []
        self.cards: list[dict] = []
        self.answer = None
        self.live: LangGraphConfig | None = None
        monkeypatch.setattr(HOST, "apply_settings", self._apply)
        monkeypatch.setattr(HOST, "config", lambda: self.live)
        monkeypatch.setattr(langgraph.types, "interrupt", self._interrupt)

    def _apply(self, patch):
        self.patches.append(copy.deepcopy(patch))
        if self.live is not None:
            if "projects" in patch:
                self.live.projects = patch["projects"]
            if "projects" in patch.get("filesystem", {}):
                self.live.filesystem_projects = patch["filesystem"]["projects"]
        return True, ["ok"]

    def _interrupt(self, card):
        self.cards.append(card)
        if self.answer is None:
            raise AssertionError("no approval card may be shown here")
        if self.answer in (ALLOW, KEEP):
            return next(o["value"] for o in card["options"] if o["label"] == self.answer)
        return self.answer

    async def call(self, config, tool, **args):
        self.live = config
        tools = {t.name: t for t in onboard_tools.build_onboard_tools(config)}
        return await tools[tool].ainvoke(args)


@pytest.fixture
def host(monkeypatch):
    return _Host(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # Windows temp repos live under the runner's ~/AppData, which the outside-root
    # guard correctly refuses. Keep the test projects outside the simulated home,
    # just as test_onboard_outside_root does, without bypassing the real guard.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture
def root(tmp_path):
    (tmp_path / "code").mkdir()
    return tmp_path / "code"


# Each case: (tool, where the checkout sits, the call's arguments given its path).
CASES = [
    ("onboard_project", "inside", lambda p: {"repo": "acme/munda"}),
    ("onboard_project", "outside", lambda p: {"repo": "acme/munda"}),
    ("register_local_project", "inside", lambda p: {"path": str(p)}),
    ("register_local_project", "outside", lambda p: {"path": str(p)}),
]
case = pytest.mark.parametrize(("tool", "where", "args"), CASES)


def _place(root: Path, tmp_path: Path, where: str) -> Path:
    return _repo((root if where == "inside" else tmp_path / "elsewhere") / "munda")


# ── raising: the card, and the approved write ───────────────────────────────


@case
@pytest.mark.parametrize("mirrored", [False, True])
async def test_write_true_on_a_read_only_project_asks_then_raises_it(root, tmp_path, host, tool, where, args, mirrored):
    path = _place(root, tmp_path, where)
    other = {"name": "other", "path": str(root / "other"), "write": False, "no_delete": True}
    fence = [other.copy(), {"name": "munda", "path": str(path), "write": False}] if mirrored else []
    config = _cfg(root, projects=[other.copy(), _entry(path)], filesystem_projects=fence)
    host.answer = ALLOW
    out = await host.call(config, tool, write=True, **args(path))

    [card] = host.cards
    token = onboard_tools._path_token(path)
    assert (card["kind"], card["title"], card["tool"]) == ("approval", "Make this project read-write?", tool)
    assert card["path"] == str(path) and card["session_allow"] is False
    assert [(o["label"], o["value"], o["kind"]) for o in card["options"]] == [
        (ALLOW, f"allow-read-write@{token}", "allow_once"),
        (KEEP, "deny", "reject_once"),
    ]
    for line in (f"Folder:     {path}", "Name:       'munda'", "origin git@github.com:acme/munda.git"):
        assert line in card["detail"]
    assert "Access now: read-only" in card["detail"] and "Agent asks: read-write" in card["detail"]

    [patch] = host.patches
    assert patch["projects"] == [other, _entry(path, write=True)]
    if mirrored:
        assert patch["filesystem"] == {"projects": [other, {"name": "munda", "path": str(path), "write": True}]}
    else:
        assert "filesystem" not in patch
    assert f"munda at {path} is now read-write (approved by the operator)" in out


@case
@pytest.mark.parametrize(
    ("answer", "may_approve", "why"),
    [
        (KEEP, True, "denied by the operator"),
        ("", True, "denied by the operator"),  # a dismissal
        ("__autonomous__", True, "denied by the operator"),
        ("allow-read-only@x", True, "a different folder"),
        ("allow-read-write@000000000000", True, "a different folder"),  # stale token
        ("approved", True, "plain approve"),
        (True, True, "plain approve"),
        ({"approved": True}, True, "plain approve"),
        (ALLOW, False, "caller can't give it"),
    ],
)
async def test_anything_but_the_bound_allow_writes_nothing(root, tmp_path, host, tool, where, args, answer, may_approve, why):
    path = _place(root, tmp_path, where)
    config = _cfg(root, projects=[_entry(path)])
    host.answer = answer
    with request_metadata_scope({"bypass_permissions": True}, may_approve=may_approve):
        out = await host.call(config, tool, write=True, **args(path))
    assert len(host.cards) == 1  # /bypass does not skip the card
    assert host.patches == [] and config.projects == [_entry(path)]
    assert "still read-only — nothing changed" in out and why in out
    assert "Settings ▸ Projects" in out and "now read-write" not in out


async def test_the_real_autonomous_sentinel_is_a_denial(root, host):
    from server.turn_control import _AUTONOMOUS_HITL_SENTINEL

    path = _repo(root / "munda")
    host.answer = _AUTONOMOUS_HITL_SENTINEL
    out = await host.call(_cfg(root, projects=[_entry(path)]), "onboard_project", repo="acme/munda", write=True)
    assert host.patches == [] and "nothing changed" in out


async def test_no_turn_to_park_is_a_refusal_not_a_crash(root, host, monkeypatch):
    monkeypatch.undo()  # the real interrupt: outside a graph run it cannot park
    calls: list[dict] = []
    monkeypatch.setattr(HOST, "apply_settings", lambda patch: (calls.append(patch), (True, []))[1])
    monkeypatch.setattr(HOST, "config", None)
    path = _repo(root / "munda")
    tools = {t.name: t for t in onboard_tools.build_onboard_tools(_cfg(root, projects=[_entry(path)]))}
    out = await tools["onboard_project"].ainvoke({"repo": "acme/munda", "write": True})
    assert "still read-only — nothing changed" in out and "can't be shown from here" in out and calls == []


# ── lowering, and the unchanged paths ───────────────────────────────────────


@case
async def test_write_false_lowers_a_read_write_project_without_a_card(root, tmp_path, host, tool, where, args):
    path = _place(root, tmp_path, where)
    out = await host.call(_cfg(root, projects=[_entry(path, write=True)]), tool, write=False, **args(path))
    assert host.cards == []
    assert host.patches == [{"projects": [_entry(path)]}]
    assert f"munda at {path} is now read-only (lowered at your request" in out


@case
@pytest.mark.parametrize("mode", [False, True])
@pytest.mark.parametrize("omit", [False, True])
async def test_write_omitted_or_equal_changes_nothing(root, tmp_path, host, tool, where, args, mode, omit):
    path = _place(root, tmp_path, where)
    extra = {} if omit else {"write": mode}
    out = await host.call(_cfg(root, projects=[_entry(path, write=mode)]), tool, **extra, **args(path))
    assert host.cards == [] and host.patches == []
    assert f"munda is already registered at {path} (" in out and "nothing changed" in out
    if not omit:  # (omitted, inside the root: the reply names the default mode — as before)
        assert f"({'read-write' if mode else 'read-only'})" in out


@case
async def test_round_trip_read_only_to_read_write_and_back(root, tmp_path, host, tool, where, args):
    path = _place(root, tmp_path, where)
    config = _cfg(root, projects=[_entry(path)])
    host.answer = ALLOW
    assert "now read-write" in await host.call(config, tool, write=True, **args(path))
    assert "now read-only" in await host.call(config, tool, write=False, **args(path))
    assert "(read-only)" in await host.call(config, tool, write=False, **args(path))  # equal → no-op
    assert len(host.cards) == 1
    assert host.patches == [{"projects": [_entry(path, write=True)]}, {"projects": [_entry(path)]}]


# ── fence-only entries ──────────────────────────────────────────────────────


async def test_fence_only_entry_is_raised_in_the_fence_then_promoted_at_that_mode(root, tmp_path, host):
    path = _repo(tmp_path / "elsewhere" / "munda")
    fence = [{"name": "munda", "path": str(path), "write": False}]
    host.answer = ALLOW
    out = await host.call(_cfg(root, filesystem_projects=fence), "register_local_project", path=str(path), write=True)
    change, promote = host.patches
    assert change == {"filesystem": {"projects": [{"name": "munda", "path": str(path), "write": True}]}}
    assert promote["projects"][0]["write"] is True and "projects" not in promote.get("filesystem", {})
    assert "now read-write (approved by the operator) — updated in the explicit filesystem.projects override" in out
    assert "Promoted the existing filesystem.projects entry for munda (read-write)" in out


async def test_fence_only_entry_without_a_mode_change_is_promoted_at_its_own_mode(root, tmp_path, host):
    path = _repo(tmp_path / "elsewhere" / "munda")
    fence = [{"name": "munda", "path": str(path), "write": False}]
    out = await host.call(_cfg(root, filesystem_projects=fence), "register_local_project", path=str(path))
    assert host.cards == [] and out.startswith("Promoted the existing filesystem.projects entry for munda (read-only)")
    assert [p["projects"][0]["write"] for p in host.patches] == [False]


# ── the helper ──────────────────────────────────────────────────────────────


async def test_helper_changes_only_write_and_keeps_every_entry_in_order(root, host):
    path = _repo(root / "munda")
    registry = [
        {"name": "a", "path": str(root / "a"), "write": True, "github": "acme/a"},
        _entry(path, no_delete=True, custom={"k": [1]}),
        {"name": "z", "path": str(root / "z")},
    ]
    fence = [{"name": "z", "path": str(root / "z"), "write": False}, {"name": "munda", "path": str(path), "tag": "x"}]
    host.live = _cfg(root, projects=copy.deepcopy(registry), filesystem_projects=copy.deepcopy(fence))
    reg = await onboard_tools.set_project_write(host.live, name="munda", path=path, write=True)

    assert (reg.status, reg.mirrored) == ("changed", True)
    [patch] = host.patches
    assert set(patch) == {"projects", "filesystem"} and set(patch["filesystem"]) == {"projects"}
    registry[1]["write"] = True
    fence[1]["write"] = True
    assert patch["projects"] == registry and patch["filesystem"]["projects"] == fence
    assert [list(e) for e in patch["projects"]] == [list(e) for e in registry]  # key order too

    again = await onboard_tools.set_project_write(host.live, name="munda", path=path, write=True)
    assert again.status == "already" and len(host.patches) == 1


async def test_helper_returns_an_error_instead_of_raising(root, host, monkeypatch):
    path = _repo(root / "munda")
    host.live = _cfg(root, projects=[_entry(path)])
    missing = await onboard_tools.set_project_write(host.live, name="nope", path=root / "nope", write=True)
    assert missing.status == "error" and "no registered project matches" in missing.error

    monkeypatch.setattr(HOST, "apply_settings", lambda patch: (False, ["disk full"]))
    assert "disk full" in (await onboard_tools.set_project_write(host.live, name="munda", path=path, write=True)).error
    monkeypatch.setattr(HOST, "apply_settings", lambda patch: 1 / 0)
    assert (await onboard_tools.set_project_write(host.live, name="munda", path=path, write=True)).status == "error"
    monkeypatch.setattr(HOST, "apply_settings", None)
    assert "not wired" in (await onboard_tools.set_project_write(host.live, name="munda", path=path, write=True)).error

    # ...and a tool whose approved change can't be applied says so, and that nothing changed.
    host.answer = ALLOW
    out = await host.call(host.live, "register_local_project", path=str(path), write=True)
    assert out.startswith("Error: approved by the operator, but") and "Nothing changed" in out


def test_delegate_read_only_refusal_names_the_approval_card(root, monkeypatch):
    from plugins.delegates.adapters import DelegateError
    from plugins.delegates.projects import resolve

    cfg = _cfg(root, filesystem_enabled=True, projects=[_entry(_repo(root / "munda"))])
    monkeypatch.setattr(HOST, "config", lambda: cfg)
    with pytest.raises(DelegateError) as exc:
        resolve("munda")
    msg = str(exc.value)
    assert "is read-only (write: false)" in msg
    assert "onboard_project / register_local_project with write=true" in msg and "approval card" in msg
    assert "Settings ▸ Projects" in msg
