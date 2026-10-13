"""``register_local_project`` OUTSIDE ``onboarding.root``: the operator approval card.

A folder outside the root no longer gets a flat refusal (``onboarding.approve_outside_root``,
default on): the tool parks on a hitl-v1 ``approval`` with Allow read-only / Allow
read-write / Deny, and only the operator's answer registers it. Pinned here:

  - inside the root → registers directly, no card
  - outside → the park payload: the REALPATH, git/origin facts, the three options bound
    to a digest of that realpath, ``session_allow: false``
  - resume allow-ro / allow-rw → registered at THAT mode (the operator's choice beats
    the agent's ``write``); deny / dismissal / autonomous sentinel → nothing written
  - an answer bound to a different path (the folder changed while the card was open)
    → nothing written
  - the hard floor (filesystem root, home and its ancestors, ~/Library, credential dirs,
    system dirs, temp dirs themselves, protoAgent's own state) never parks
  - /bypass does not skip the card
  - symlinks: the card and the registry carry the realpath, never the link
  - idempotent re-register → no card; a fence-only entry is promoted at ITS mode
  - config off → the old refusal
  - a REAL LangGraph park → resume round trip (the tool re-runs from the top)

Real temp directories and real git repos throughout; only the config writer
(``HOST.apply_settings``) is faked, and ``interrupt`` is stubbed in the unit tests to
record the payload and answer it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import langgraph.types
import pytest

from graph.config import LangGraphConfig
from graph.middleware.request_context import request_metadata_scope
from graph.plugins.host import HOST
from tools import onboard_tools

pytestmark = pytest.mark.platform_sensitive

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX system-directory layout")


def _git(*argv, cwd):
    subprocess.run(["git", *argv], cwd=str(cwd), check=True, capture_output=True)


def _repo(path: Path, *, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "trunk", cwd=path)
    if origin:
        _git("remote", "add", "origin", origin, cwd=path)
    return path


def _cfg(root: Path, **over) -> LangGraphConfig:
    kw = dict(
        onboarding_enabled=True,
        onboarding_root=str(root),
        onboarding_allow=[],
        onboarding_write_default=False,
    )
    kw.update(over)
    return LangGraphConfig(**kw)


def _tool(config: LangGraphConfig):
    return {t.name: t for t in onboard_tools.build_onboard_tools(config)}["register_local_project"]


@pytest.fixture
def applied(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(HOST, "apply_settings", lambda patch: (calls.append(patch), (True, ["ok"]))[1])
    monkeypatch.setattr(HOST, "config", None)
    return calls


class _Park:
    """Stand-in for ``langgraph.types.interrupt``: records every payload and answers
    with ``answer`` (a callable of the payload, or a fixed value)."""

    def __init__(self, answer=None):
        self.payloads: list[dict] = []
        self.answer = answer

    def __call__(self, payload):
        self.payloads.append(payload)
        if self.answer is None:
            raise AssertionError("the approval card must not be shown here")
        return self.answer(payload) if callable(self.answer) else self.answer


def _option(label: str):
    return lambda payload: next(o["value"] for o in payload["options"] if o["label"] == label)


@pytest.fixture
def park(monkeypatch):
    p = _Park()
    monkeypatch.setattr(langgraph.types, "interrupt", p)
    return p


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    outside = _repo(tmp_path / "elsewhere" / "munda", origin="git@github.com:acme/munda.git")
    return root, outside


# ── inside the root: unchanged, no card ─────────────────────────────────────


async def test_inside_root_registers_without_a_card(layout, applied, park):
    root, _ = layout
    inside = _repo(root / "widget")
    out = await _tool(_cfg(root)).ainvoke({"path": str(inside)})
    assert out.startswith("Registered widget (read-only)")
    assert park.payloads == []  # _Park(None) would have raised
    assert applied[0]["projects"][0]["path"] == str(inside.resolve())


# ── outside: the park payload ───────────────────────────────────────────────


async def test_outside_root_parks_with_the_realpath_and_three_bound_options(layout, applied, park):
    root, outside = layout
    park.answer = _DENY_ANSWER
    await _tool(_cfg(root)).ainvoke({"path": str(outside), "write": True})

    [card] = park.payloads
    real = str(outside.resolve())
    assert card["kind"] == "approval"
    assert card["tool"] == "register_local_project"
    assert card["path"] == real
    assert card["session_allow"] is False
    assert card["title"] == "Allow access to a folder outside the onboarding root?"
    detail = card["detail"]
    assert f"Folder:     {real}" in detail
    assert "git checkout, origin git@github.com:acme/munda.git" in detail
    assert "Agent asks: read-write" in detail
    assert str(root) in detail
    token = onboard_tools._path_token(outside.resolve())
    assert [(o["label"], o["value"], o["kind"]) for o in card["options"]] == [
        ("Allow read-only", f"allow-read-only@{token}", "allow_once"),
        ("Allow read-write", f"allow-read-write@{token}", "allow_once"),
        ("Deny", "deny", "reject_once"),
    ]
    assert not any(o["kind"] == "allow_always" for o in card["options"])
    assert applied == []


async def test_card_reports_a_plain_folder_and_redacts_origin_credentials(tmp_path, applied, park):
    root = tmp_path / "code"
    root.mkdir()
    plain = tmp_path / "notes"
    plain.mkdir()
    tokened = _repo(tmp_path / "tok", origin="https://user:s3cret@gitlab.com/acme/tok.git")
    park.answer = _DENY_ANSWER
    tool = _tool(_cfg(root))
    await tool.ainvoke({"path": str(plain)})
    await tool.ainvoke({"path": str(tokened)})
    assert "Repository: not a git checkout" in park.payloads[0]["detail"]
    assert "Agent asks: read-only" in park.payloads[0]["detail"]  # write omitted → write_default (off)
    assert "s3cret" not in park.payloads[1]["detail"] and "user:***@gitlab.com" in park.payloads[1]["detail"]


async def test_no_git_runs_in_the_folder_before_approval(layout, applied, park, monkeypatch):
    """A hostile repo config (core.fsmonitor …) can make git execute code — so the card is
    built from reading .git/config, and git only runs after the operator says yes."""
    root, outside = layout
    ran: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv, *a, **k):
        ran.append(list(argv))
        return real_run(argv, *a, **k)

    monkeypatch.setattr(onboard_tools.subprocess, "run", spy)
    import graph.workspaces.manager as manager

    monkeypatch.setattr(manager.subprocess, "run", spy)
    park.answer = _DENY_ANSWER
    await _tool(_cfg(root)).ainvoke({"path": str(outside)})
    assert ran == []


# ── the answer ──────────────────────────────────────────────────────────────


_DENY_ANSWER = "deny"


@pytest.mark.parametrize(("label", "agent_write", "expect_write"), [
    ("Allow read-only", None, False),
    ("Allow read-write", None, True),
    ("Allow read-only", True, False),   # the operator's choice beats the agent's write=true
    ("Allow read-write", False, True),
])  # fmt: skip
async def test_approved_registers_at_the_operators_mode(layout, applied, park, label, agent_write, expect_write):
    root, outside = layout
    park.answer = _option(label)
    args = {"path": str(outside)} if agent_write is None else {"path": str(outside), "write": agent_write}
    out = await _tool(_cfg(root)).ainvoke(args)

    [patch] = applied
    entry = patch["projects"][-1]
    assert entry == {
        "name": "munda",
        "path": str(outside.resolve()),
        "github": "acme/munda",
        "default_branch": "trunk",
        "write": expect_write,
    }
    rw = "read-write" if expect_write else "read-only"
    assert f"approved by the operator ({rw})" in out
    if agent_write is not None and agent_write != expect_write:
        assert "the operator's choice wins" in out


@pytest.mark.parametrize("answer", ["deny", "denied", "", "garbage", "allow-read-only", False, {"approved": False}])
async def test_denied_or_unrecognized_registers_nothing(layout, applied, park, answer):
    root, outside = layout
    park.answer = answer
    out = await _tool(_cfg(root)).ainvoke({"path": str(outside)})
    assert out.startswith("Denied by the operator") and "onboard_project" in out
    assert applied == []


async def test_autonomous_sentinel_is_a_denial(layout, applied, park):
    from server.chat import _AUTONOMOUS_HITL_SENTINEL

    root, outside = layout
    park.answer = _AUTONOMOUS_HITL_SENTINEL
    out = await _tool(_cfg(root)).ainvoke({"path": str(outside)})
    assert out.startswith("Denied") and applied == []


async def test_answer_bound_to_another_path_is_refused(layout, applied, park):
    root, outside = layout
    park.answer = "allow-read-write@000000000000"
    out = await _tool(_cfg(root)).ainvoke({"path": str(outside)})
    assert out.startswith("Not registered: the approval answered a different folder")
    assert applied == []


@pytest.mark.parametrize("answer", ["approved", "approve", True, {"approved": True}, {"decision": "approve"}])
async def test_plain_approve_registers_nothing(layout, applied, park, answer):
    """A plain approve is what an OLD client sends — and what every auto-approver sends
    (an older Zed shim's "Allow for this session", "Approve & don't ask again"). It can't
    be bound to the folder or told apart from a standing yes, so nothing is written."""
    root, outside = layout
    park.answer = answer
    out = await _tool(_cfg(root)).ainvoke({"path": str(outside), "write": True})
    assert out.startswith("Not registered:") and "plain approve" in out
    assert applied == []


async def test_bypass_does_not_skip_the_card(layout, applied, park):
    root, outside = layout
    park.answer = _DENY_ANSWER
    with request_metadata_scope({"bypass_permissions": True}):
        out = await _tool(_cfg(root, filesystem_bypass_allowed=True)).ainvoke({"path": str(outside)})
    assert len(park.payloads) == 1 and out.startswith("Denied")
    assert applied == []


# ── the hard floor: never a card ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """Every test gets its own home dir. Besides letting the home-relative floor be tested
    for real, it keeps the RUNNER's home out of it: on Windows ``tmp_path`` lives under
    ``%USERPROFILE%\\AppData``, which the floor refuses."""
    home = tmp_path / "home" / "kj"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda h, t: h, "home directory"),
        (lambda h, t: h.parent, "contains your home directory"),
        (lambda h, t: h / "Library" / "Application Support" / "x", "~/Library"),
        (lambda h, t: h / ".config", "~/.config"),
        (lambda h, t: h / ".ssh", "credentials directory"),
        (lambda h, t: t / "proj" / ".aws" / "cli", "credentials directory"),
        (lambda h, t: t / "proj" / ".gnupg", "credentials directory"),
    ],
)
async def test_hard_refusals_under_home_never_park(tmp_path, fake_home, applied, park, make, why):
    root = tmp_path / "code"
    root.mkdir()
    target = make(fake_home, tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    out = await _tool(_cfg(root)).ainvoke({"path": str(target)})
    assert out.startswith("Refused:") and why in out and "even with the operator's approval" in out
    assert park.payloads == [] and applied == []


@posix_only
@pytest.mark.parametrize("path", ["/", "/etc", "/usr/bin", "/tmp", "/var/tmp", "/bin"])
async def test_system_paths_never_park(tmp_path, applied, park, path):
    if not Path(path).exists():
        pytest.skip(f"{path} absent here")
    root = tmp_path / "code"
    root.mkdir()
    out = await _tool(_cfg(root)).ainvoke({"path": path})
    assert out.startswith("Refused:") and "even with the operator's approval" in out
    assert park.payloads == [] and applied == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS layout")
@pytest.mark.parametrize("path", ["/System", "/Library/Preferences", "/private/etc", "/Applications", "/Users"])
async def test_macos_system_paths_never_park(tmp_path, applied, park, path):
    if not Path(path).exists():
        pytest.skip(f"{path} absent here")
    root = tmp_path / "code"
    root.mkdir()
    out = await _tool(_cfg(root)).ainvoke({"path": path})
    assert out.startswith("Refused:")
    assert park.payloads == [] and applied == []


async def test_protoagent_state_dirs_never_park(tmp_path, applied, park, monkeypatch):
    root = tmp_path / "code"
    root.mkdir()
    state = tmp_path / "state" / "box"
    (state / "default").mkdir(parents=True)
    monkeypatch.setattr(onboard_tools, "_protoagent_state_roots", lambda: [state.resolve()])
    tool = _tool(_cfg(root))
    for target in (state / "default", state.parent):  # inside it, and containing it
        out = await tool.ainvoke({"path": str(target)})
        assert out.startswith("Refused:") and "protoAgent's own config" in out
    assert park.payloads == [] and applied == []


@posix_only
async def test_control_characters_in_the_path_never_park(tmp_path, applied, park):
    root = tmp_path / "code"
    root.mkdir()
    sneaky = tmp_path / "ok\nFolder:     safe"
    sneaky.mkdir()
    out = await _tool(_cfg(root)).ainvoke({"path": str(sneaky)})
    assert out.startswith("Refused:") and "control characters" in out
    assert park.payloads == [] and applied == []


@posix_only
@pytest.mark.parametrize("name", ["munda\u202egnp.exe", "mun\u200bda"])
async def test_bidi_and_zero_width_characters_in_the_path_never_park(tmp_path, applied, park, name):
    root = tmp_path / "code"
    root.mkdir()
    (tmp_path / name).mkdir()
    out = await _tool(_cfg(root)).ainvoke({"path": str(tmp_path / name)})
    assert out.startswith("Refused:") and "control characters" in out
    assert park.payloads == [] and applied == []


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive default filesystem")
@pytest.mark.parametrize("path", ["/USR/bin", "/eTc", "/SYSTEM"])
async def test_recased_system_paths_are_still_refused_on_macos(tmp_path, applied, park, path):
    """resolve() keeps the caller's casing on macOS, so the floor must compare case-folded."""
    root = tmp_path / "code"
    root.mkdir()
    out = await _tool(_cfg(root)).ainvoke({"path": path})
    assert out.startswith("Refused:") and "even with the operator's approval" in out
    assert park.payloads == [] and applied == []


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive default filesystem")
async def test_recased_home_is_still_refused_on_macos(tmp_path, applied, park, fake_home):
    root = tmp_path / "code"
    root.mkdir()
    out = await _tool(_cfg(root)).ainvoke({"path": str(fake_home).upper()})
    assert out.startswith("Refused:") and "home directory" in out
    assert park.payloads == [] and applied == []


async def test_name_cannot_forge_card_lines(layout, applied, park):
    root, outside = layout
    out = await _tool(_cfg(root)).ainvoke({"path": str(outside), "name": "x\nFolder:     /safe"})
    assert out.startswith("Error: name must be")
    assert park.payloads == [] and applied == []


async def test_missing_and_file_targets_error_without_a_card(tmp_path, applied, park):
    root = tmp_path / "code"
    root.mkdir()
    (tmp_path / "a-file").write_text("x")
    tool = _tool(_cfg(root))
    for p in (tmp_path / "nope", tmp_path / "a-file"):
        out = await tool.ainvoke({"path": str(p)})
        assert out.startswith("Error:") and "not an existing directory" in out
    assert park.payloads == [] and applied == []


# ── symlinks: the realpath is what is shown and what is registered ──────────


async def test_symlink_in_root_to_outside_shows_and_registers_the_realpath(layout, applied, park):
    root, outside = layout
    link = root / "innocent"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without developer mode
        pytest.skip(f"symlinks unavailable: {exc}")
    park.answer = _option("Allow read-only")
    await _tool(_cfg(root)).ainvoke({"path": str(link)})

    [card] = park.payloads
    real = str(outside.resolve())
    assert card["path"] == real and f"Folder:     {real}" in card["detail"]
    assert "Requested:  " in card["detail"] and "innocent" in card["detail"]  # labelled as what was typed
    assert applied[0]["projects"][-1]["path"] == real


@posix_only
async def test_symlink_to_a_system_dir_is_hard_refused(tmp_path, applied, park):
    root = tmp_path / "code"
    root.mkdir()
    (root / "cfg").symlink_to("/etc", target_is_directory=True)
    out = await _tool(_cfg(root)).ainvoke({"path": str(root / "cfg")})
    assert out.startswith("Refused:") and "system directory" in out
    assert park.payloads == [] and applied == []


# ── idempotency / promotion / config off ────────────────────────────────────


async def test_already_registered_outside_folder_needs_no_card(layout, applied, park):
    root, outside = layout
    entry = {"name": "munda", "path": str(outside.resolve()), "write": False}
    out = await _tool(_cfg(root, projects=[entry])).ainvoke({"path": str(outside), "write": True})
    assert "already registered" in out and "(read-only)" in out
    assert park.payloads == [] and applied == []


async def test_fence_only_outside_entry_is_promoted_at_its_own_mode(layout, applied, park):
    root, outside = layout
    fence = {"name": "munda", "path": str(outside.resolve()), "write": False}
    out = await _tool(_cfg(root, filesystem_projects=[fence])).ainvoke({"path": str(outside), "write": True})
    assert out.startswith("Promoted")
    assert applied[0]["projects"][-1]["write"] is False  # the agent's write=true did not upgrade it
    assert park.payloads == []


async def test_config_off_restores_the_hard_refusal(layout, applied, park):
    root, outside = layout
    out = await _tool(_cfg(root, onboarding_approve_outside_root=False)).ainvoke({"path": str(outside)})
    assert out.startswith("Refused:") and "outside the onboarding root" in out
    assert park.payloads == [] and applied == []


def test_config_key_round_trips():
    assert LangGraphConfig.from_dict({}).onboarding_approve_outside_root is True
    cfg = LangGraphConfig.from_dict({"onboarding": {"approve_outside_root": False}})
    assert cfg.onboarding_approve_outside_root is False


# ── a real LangGraph park → resume ──────────────────────────────────────────


async def _drive(tool, args, answer):
    """Run ``tool`` inside a real checkpointed graph: park, then resume with ``answer``.
    Returns (the parked payload, the tool's final result)."""
    from typing import TypedDict

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    class S(TypedDict, total=False):
        out: str

    async def node(_state: S) -> S:
        return {"out": await tool.ainvoke(args)}

    g = StateGraph(S)
    g.add_node("call", node)
    g.add_edge(START, "call")
    g.add_edge("call", END)
    graph = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t1"}}
    first = await graph.ainvoke({}, cfg)
    [intr] = first["__interrupt__"]
    value = answer(intr.value) if callable(answer) else answer
    final = await graph.ainvoke(Command(resume=value), cfg)
    return intr.value, final["out"]


async def test_real_graph_park_and_resume_registers_read_write(layout, applied):
    root, outside = layout
    payload, out = await _drive(_tool(_cfg(root)), {"path": str(outside)}, _option("Allow read-write"))
    assert payload["path"] == str(outside.resolve())
    assert "approved by the operator (read-write)" in out
    assert applied[0]["projects"][-1]["write"] is True


async def test_real_graph_park_and_deny_registers_nothing(layout, applied):
    root, outside = layout
    _payload, out = await _drive(_tool(_cfg(root)), {"path": str(outside)}, "deny")
    assert out.startswith("Denied by the operator") and applied == []


# ── no onboarding root at all (a stock install): the same card ──────────────


async def test_unset_root_parks_with_the_no_root_line(layout, applied, park):
    _root, outside = layout
    park.answer = _DENY_ANSWER
    out = await _tool(_cfg(Path(""), onboarding_root="")).ainvoke({"path": str(outside)})
    [card] = park.payloads
    assert card["path"] == str(outside.resolve()) and card["session_allow"] is False
    assert "Outside:    no onboarding root is set — this agent has no default workspace" in card["detail"]
    assert "approving registers only this folder" in card["detail"]
    assert "onboarding root ." not in card["detail"]
    assert out.startswith("Denied by the operator") and "with no onboarding root set" in out
    assert applied == []


@pytest.mark.parametrize(("label", "expect_write"), [("Allow read-only", False), ("Allow read-write", True)])
async def test_unset_root_approved_registers_at_the_chosen_access(layout, applied, park, label, expect_write):
    _root, outside = layout
    park.answer = _option(label)
    out = await _tool(_cfg(Path(""), onboarding_root="")).ainvoke({"path": str(outside)})
    entry = applied[0]["projects"][-1]
    assert entry["path"] == str(outside.resolve()) and entry["write"] is expect_write
    rw = "read-write" if expect_write else "read-only"
    assert f"approved by the operator ({rw})" in out and "with no onboarding root set" in out


async def test_unset_root_real_graph_round_trip(layout, applied):
    _root, outside = layout
    payload, out = await _drive(_tool(_cfg(Path(""), onboarding_root="")), {"path": str(outside)}, _option("Allow read-only"))
    assert "no onboarding root is set" in payload["detail"]
    assert applied[0]["projects"][-1]["write"] is False and "approved by the operator" in out


@pytest.mark.parametrize(
    "make",
    [lambda h, t: h, lambda h, t: h / ".ssh", lambda h, t: h / "Library" / "x", lambda h, t: t / "p" / ".aws"],
)
async def test_unset_root_hard_refusals_still_never_park(tmp_path, fake_home, applied, park, make):
    target = make(fake_home, tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    out = await _tool(_cfg(Path(""), onboarding_root="")).ainvoke({"path": str(target)})
    assert out.startswith("Refused:") and "even with the operator's approval" in out
    assert "with no onboarding root set" in out
    assert park.payloads == [] and applied == []


@posix_only
async def test_unset_root_system_dir_still_refused(applied, park):
    out = await _tool(_cfg(Path(""), onboarding_root="")).ainvoke({"path": "/etc"})
    assert out.startswith("Refused:") and "system directory" in out
    assert park.payloads == [] and applied == []


async def test_unset_root_with_config_off_is_the_old_refusal(layout, applied, park):
    _root, outside = layout
    out = await _tool(_cfg(Path(""), onboarding_root="", onboarding_approve_outside_root=False)).ainvoke(
        {"path": str(outside)}
    )
    assert out.startswith("Refused:") and "onboarding.root isn't set" in out
    assert park.payloads == [] and applied == []


async def test_unset_root_bypass_does_not_skip_the_card(layout, applied, park):
    _root, outside = layout
    park.answer = _DENY_ANSWER
    with request_metadata_scope({"bypass_permissions": True}):
        await _tool(_cfg(Path(""), onboarding_root="")).ainvoke({"path": str(outside)})
    assert len(park.payloads) == 1 and applied == []


async def test_unset_root_onboard_project_still_refuses_to_clone(tmp_path, applied, park):
    """Cloning is unchanged: with no root there is nowhere for a clone to land."""
    tools = {t.name: t for t in onboard_tools.build_onboard_tools(_cfg(Path(""), onboarding_root="", onboarding_allow=["github.com/acme/*"]))}
    out = await tools["onboard_project"].ainvoke({"repo": "acme/widget"})
    assert out.startswith("Refused:") and "onboarding.root isn't set" in out
    assert park.payloads == [] and applied == []
