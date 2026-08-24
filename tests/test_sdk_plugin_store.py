"""`sdk.plugin_store` — the seam that replaces four hand-rolled path resolvers.

Before this existed, every plugin needing persistence wrote its own `_data_dir()`, and
they diverged: one hardcoded an absolute sandbox path, one imported a host symbol that had
been deleted (silently disabling its feature), one re-implemented instance scoping by hand.
The tests below pin the properties those attempts each got wrong in a different way —
instance scoping, and refusing to write outside the plugin's own directory.
"""

from __future__ import annotations

import pytest

from graph import sdk


def test_returns_an_existing_dir_scoped_to_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "inst"))
    from infra.paths import reset_instance_paths

    reset_instance_paths()

    d = sdk.plugin_store(plugin_id="my-plugin")

    assert d.is_dir(), "callers open files immediately — the directory must already exist"
    assert d.name == "my-plugin"
    assert str(tmp_path) in str(d), "must live under the instance root, not the process CWD"


def test_two_instances_never_share_a_store(tmp_path, monkeypatch):
    """The isolation each plugin used to have to remember (ADR 0004 / ADR 0065)."""
    from infra.paths import reset_instance_paths

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "default"))
    reset_instance_paths()
    a = sdk.plugin_store(plugin_id="p")

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "dev"))
    reset_instance_paths()
    b = sdk.plugin_store(plugin_id="p")

    assert a != b, "the dev sandbox must not write into the default instance's store"


def test_subdir_nests_inside_the_plugin_store(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "inst"))
    from infra.paths import reset_instance_paths

    reset_instance_paths()

    d = sdk.plugin_store("cache/thumbs", plugin_id="p")

    assert d.is_dir()
    assert d.parent.parent == sdk.plugin_store(plugin_id="p")


@pytest.mark.parametrize("bad", ["", "  ", "a:b", "a/b", "..", "."])
def test_unsafe_plugin_id_is_refused(bad):
    """A bad id must raise, never silently resolve somewhere else — writing an operator's
    data to an unexpected place is worse than failing."""
    with pytest.raises(ValueError):
        sdk.plugin_store(plugin_id=bad)


@pytest.mark.parametrize("bad", ["../escape", "a/../../escape", "/etc"])
def test_escaping_subdir_is_refused(bad, tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "inst"))
    from infra.paths import reset_instance_paths

    reset_instance_paths()

    with pytest.raises(ValueError):
        sdk.plugin_store(bad, plugin_id="p")
