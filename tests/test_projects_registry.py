"""The ADR 0095 managed-projects registry and its projection onto the fs fence.

Covers ``LangGraphConfig.projects`` (parse + round-trip) and the two accessors
that project it: ``fenced_projects`` (registry → fence shape) and
``effective_filesystem_projects`` (the precedence the agent actually gets).
"""

import textwrap
from pathlib import Path

import pytest

from graph.config import LangGraphConfig
from graph.config_io import config_to_dict


def _cfg(**kw) -> LangGraphConfig:
    return LangGraphConfig(**kw)


def _r(path: str) -> str:
    """The path fenced_projects() will PROJECT for this input.

    It resolves (matching tools/fs_tools.py and the OpenShell policy), and on macOS
    /tmp is itself a symlink to /private/tmp — so a hard-coded expectation would be
    asserting the unresolved value the QA panel flagged as the bug."""
    return str(Path(path).resolve())


# ---------------------------------------------------------------------------
# Parse + round-trip
# ---------------------------------------------------------------------------


def test_projects_parses_from_yaml(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(
        textwrap.dedent("""
        projects:
          - name: protoAgent
            path: /tmp/pa
            github: protoLabsAI/protoAgent
            default_branch: main
            write: true
        """).strip()
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.projects == [
        {
            "name": "protoAgent",
            "path": "/tmp/pa",
            "github": "protoLabsAI/protoAgent",
            "default_branch": "main",
            "write": True,
        }
    ]


def test_projects_absent_is_empty_list(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("model:\n  name: gpt-4\n")
    assert LangGraphConfig.from_yaml(p).projects == []


def test_projects_emitted_by_config_to_dict():
    """§B emission — a consumer treating the dict as the complete config keeps it."""
    entry = {"name": "a", "path": "/tmp/a", "github": "o/a"}
    assert config_to_dict(_cfg(projects=[entry]))["projects"] == [entry]


# ---------------------------------------------------------------------------
# fenced_projects — registry → fence shape
# ---------------------------------------------------------------------------


def test_registered_is_read_write_by_default():
    """ADR 0095 D3: registration grants read-write unless it opts out."""
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a"}])
    assert cfg.fenced_projects() == [{"name": "a", "path": _r("/tmp/a"), "write": True}]


def test_write_false_projects_read_only():
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "write": False}])
    assert cfg.fenced_projects() == [{"name": "a", "path": _r("/tmp/a"), "write": False}]


def test_no_delete_carries_through():
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "no_delete": True}])
    assert cfg.fenced_projects() == [
        {"name": "a", "path": _r("/tmp/a"), "write": True, "no_delete": True}
    ]


def test_fs_false_opts_out_of_the_fence():
    """Registered for github/board purposes, no filesystem reach."""
    cfg = _cfg(
        projects=[
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "fs": False, "github": "o/b"},
        ]
    )
    assert [p["name"] for p in cfg.fenced_projects()] == ["a"]


def test_identity_fields_are_stripped_from_the_fence():
    """github/default_branch are identity for other consumers — not fence vocabulary."""
    cfg = _cfg(
        projects=[
            {
                "name": "a",
                "path": "/tmp/a",
                "github": "o/a",
                "default_branch": "main",
            }
        ]
    )
    assert cfg.fenced_projects() == [{"name": "a", "path": _r("/tmp/a"), "write": True}]


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "/tmp/a"},  # no name
        {"name": "a"},  # no path
        {"name": "", "path": "/tmp/a"},  # blank name
        "not-a-dict",
    ],
)
def test_malformed_entries_are_skipped(entry):
    assert _cfg(projects=[entry]).fenced_projects() == []


# ---------------------------------------------------------------------------
# effective_filesystem_projects — the precedence the agent actually gets
# ---------------------------------------------------------------------------


def test_explicit_filesystem_projects_win_over_the_registry():
    """Non-regressing: an existing config keeps its exact fence."""
    explicit = [{"name": "legacy", "path": "/tmp/legacy", "write": False}]
    cfg = _cfg(
        filesystem_projects=explicit,
        projects=[{"name": "a", "path": "/tmp/a"}],
    )
    assert cfg.effective_filesystem_projects() == explicit


def test_registry_projects_onto_the_fence_when_no_explicit_list():
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "write": False}])
    assert cfg.effective_filesystem_projects() == [
        {"name": "a", "path": _r("/tmp/a"), "write": False}
    ]


def test_filesystem_disabled_yields_no_fence_even_with_a_registry():
    cfg = _cfg(filesystem_enabled=False, projects=[{"name": "a", "path": "/tmp/a"}])
    assert cfg.effective_filesystem_projects() == []


def test_all_entries_opted_out_yields_NO_fence():
    """`fs: false` means "registered, but NOT in the fence at all" (D3) and is now
    honoured literally. This previously fell through to the workspace default —
    granting read-write on a directory the operator had just said they didn't want,
    to avoid an invisible unbind (#2251). The unbind is no longer invisible: the
    drops WARN and /api/projects reports fence_source "unbound"."""
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "fs": False}])
    assert cfg.effective_filesystem_projects() == []


def test_a_registry_of_only_bad_entries_yields_NO_fence():
    """Same rule for the mistake case: a configured registry is the answer even when
    every entry is unusable. The warnings name each one, so this is a loud empty
    fence rather than a silent substitution."""
    cfg = _cfg(projects=[{"name": "a", "path": "relative/path"}])
    assert cfg.effective_filesystem_projects() == []


def test_empty_registry_keeps_the_workspace_default():
    """The DEFAULT-INSTALL path, deliberately unchanged: no registry configured at
    all still gets the fenced workspace, so nobody who hasn't opted in is affected."""
    assert [p["name"] for p in _cfg().effective_filesystem_projects()] == ["workspace"]


def test_explicit_filesystem_projects_still_win_over_an_opted_out_registry():
    """The non-regression guarantee holds even in the new empty-fence case."""
    explicit = [{"name": "legacy", "path": "/tmp/legacy", "write": True}]
    cfg = _cfg(
        filesystem_projects=explicit,
        projects=[{"name": "a", "path": "/tmp/a", "fs": False}],
    )
    assert cfg.effective_filesystem_projects() == explicit


# ---------------------------------------------------------------------------
# Hardening (review follow-up) — every rejection is LOUD, every ambiguity
# resolves toward LESS filesystem access.
# ---------------------------------------------------------------------------


def test_duplicate_name_keeps_the_first_and_warns(caplog):
    """`_by_name` in fs_tools is a dict — a duplicate silently won before, and the
    API reported BOTH rows as fenced while only one root was reachable."""
    cfg = _cfg(
        projects=[
            {"name": "a", "path": "/tmp/first"},
            {"name": "a", "path": "/tmp/second"},
        ]
    )
    with caplog.at_level("WARNING"):
        fenced = cfg.fenced_projects()
    assert [p["path"] for p in fenced] == [_r("/tmp/first")]
    assert "duplicate project name" in caplog.text


def test_relative_path_is_refused_and_warns(caplog):
    """A relative path resolves against the SERVER's cwd — the work-folders POST
    route already refuses it for this reason; the registry now agrees."""
    with caplog.at_level("WARNING"):
        fenced = _cfg(projects=[{"name": "a", "path": "rel/ative"}]).fenced_projects()
    assert fenced == []
    assert "not absolute" in caplog.text


def test_tilde_is_expanded_so_every_consumer_agrees(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    fenced = _cfg(projects=[{"name": "a", "path": "~/dev/x"}]).fenced_projects()
    assert fenced[0]["path"] == str(tmp_path / "dev/x")
    assert "~" not in fenced[0]["path"]


def test_malformed_entries_warn_where_they_are_dropped(caplog):
    """ADR 0095 constraint: a wrong entry must be VISIBLE. These are dropped here,
    so nothing downstream can report them — this is the only place that can."""
    with caplog.at_level("WARNING"):
        assert _cfg(projects=[{"name": "a"}]).fenced_projects() == []  # no path
        assert _cfg(projects=[{"path": "/tmp/x"}]).fenced_projects() == []  # no name
        assert _cfg(projects=["not-a-dict"]).fenced_projects() == []
    assert caplog.text.count("[projects]") >= 3


@pytest.mark.parametrize("falsey", [False, "false", "False", "no", "off", "0", 0, ""])
def test_fs_opt_out_accepts_string_falses(falsey):
    """`fs: "false"` from JSON/an env overlay is a truthy STRING — it used to grant
    the fence. Granting access is the direction that must never fail open."""
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "fs": falsey}])
    assert cfg.fenced_projects() == []


@pytest.mark.parametrize("falsey", [False, "false", "no", "off", "0", 0])
def test_write_false_accepts_string_falses(falsey):
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "write": falsey}])
    assert cfg.fenced_projects()[0]["write"] is False


@pytest.mark.parametrize("truthy", [True, "true", "yes", 1])
def test_fs_truthy_stays_fenced(truthy):
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "fs": truthy}])
    assert [p["name"] for p in cfg.fenced_projects()] == ["a"]


def test_no_delete_ignores_a_string_false():
    cfg = _cfg(projects=[{"name": "a", "path": "/tmp/a", "no_delete": "false"}])
    assert "no_delete" not in cfg.fenced_projects()[0]
    on = _cfg(projects=[{"name": "a", "path": "/tmp/a", "no_delete": "true"}])
    assert on.fenced_projects()[0]["no_delete"] is True


def test_a_symlinked_path_is_projected_resolved(tmp_path):
    """QA panel finding: fs_tools and gen_openshell_policy both `.resolve()`, so
    projecting the LINK here left the reported fence pointing at the symlink while
    the enforced fence and the Landlock policy followed it to the target — the
    declared-vs-enforced divergence this registry exists to prevent."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)

    fenced = _cfg(projects=[{"name": "a", "path": str(link)}]).fenced_projects()
    assert fenced[0]["path"] == str(target.resolve())


def test_relative_paths_are_still_refused_after_resolving(caplog):
    """`.resolve()` makes EVERY path absolute (a relative one against the process
    CWD), so absoluteness has to be judged before resolving or this rejection
    silently stops working."""
    with caplog.at_level("WARNING"):
        assert _cfg(projects=[{"name": "a", "path": "rel/ative"}]).fenced_projects() == []
    assert "not absolute" in caplog.text


def test_a_malformed_opted_out_entry_still_warns(caplog):
    """QA panel finding: the `fs: false` check ran BEFORE name/path validation, so
    `{fs: false}` — junk config — was dropped in silence. A WELL-FORMED opt-out
    stays silent; that one is deliberate, not a typo."""
    with caplog.at_level("WARNING"):
        assert _cfg(projects=[{"fs": False}]).fenced_projects() == []
    assert "missing name/path" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        assert _cfg(projects=[{"name": "a", "path": "/tmp/a", "fs": False}]).fenced_projects() == []
    assert caplog.text.strip() == ""
