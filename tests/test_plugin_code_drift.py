"""A symlinked plugin serving mixed code after a branch switch is detected (#2298)."""

from __future__ import annotations

import os

import pytest

from graph.plugins import loader


@pytest.fixture(autouse=True)
def _clean_registry():
    loader._SOURCE_FINGERPRINTS.clear()
    yield
    loader._SOURCE_FINGERPRINTS.clear()


def _checkout(tmp_path, body="VALUE = 1\n"):
    """A 'live checkout' the plugin dir will symlink to."""
    src = tmp_path / "checkout"
    (src / "sub").mkdir(parents=True)
    (src / "__init__.py").write_text(body)
    (src / "sub" / "tools.py").write_text("def go():\n    return 1\n")
    return src


def _manifest(path, pid="project_board"):
    from graph.plugins.manifest import PluginManifest

    return PluginManifest(id=pid, name=pid, path=path)


def test_nothing_symlinked_means_no_tracking_and_no_warning(tmp_path):
    """A normal (copied) install is immutable in practice — it must not be stat-swept."""
    plain = _checkout(tmp_path)

    loader._record_source_fingerprint(_manifest(plain))

    assert loader._SOURCE_FINGERPRINTS == {}
    assert loader.code_drift_warning() is None


def test_symlinked_plugin_is_tracked_and_quiet_while_unchanged(tmp_path):
    src = _checkout(tmp_path)
    link = tmp_path / "plugins" / "project_board"
    link.parent.mkdir(parents=True)
    link.symlink_to(src)

    loader._record_source_fingerprint(_manifest(link))

    assert "project_board" in loader._SOURCE_FINGERPRINTS
    assert loader.code_drift_warning() is None


def test_a_branch_switch_under_the_running_process_is_detected(tmp_path):
    """The observed incident: the checkout switches branch an hour into the process,
    rewriting loop.py/store.py/__init__.py, and nothing noticed."""
    src = _checkout(tmp_path)
    link = tmp_path / "plugins" / "project_board"
    link.parent.mkdir(parents=True)
    link.symlink_to(src)
    loader._record_source_fingerprint(_manifest(link))

    # Branch switch: file contents (and mtimes) change under the live process.
    (src / "__init__.py").write_text("VALUE = 2\n")
    os.utime(src / "__init__.py", (1, 1))

    warning = loader.code_drift_warning()

    assert warning and "project_board" in warning
    assert "MIX" in warning  # names the actual hazard, not just "changed"


def test_a_new_file_appearing_is_detected(tmp_path):
    """A branch that adds a module counts too — that module imports lazily and would
    load code from a commit the rest of the process never saw."""
    src = _checkout(tmp_path)
    link = tmp_path / "plugins" / "project_board"
    link.parent.mkdir(parents=True)
    link.symlink_to(src)
    loader._record_source_fingerprint(_manifest(link))

    (src / "projects.py").write_text("NEW = True\n")

    assert loader.code_drift_warning()


def test_non_python_churn_does_not_warn(tmp_path):
    """Only importable sources matter — a README or a .pyc must not raise a false alarm
    that trains operators to ignore the banner."""
    src = _checkout(tmp_path)
    link = tmp_path / "plugins" / "project_board"
    link.parent.mkdir(parents=True)
    link.symlink_to(src)
    loader._record_source_fingerprint(_manifest(link))

    (src / "README.md").write_text("# docs\n")

    assert loader.code_drift_warning() is None


def test_reimport_clears_the_warning(tmp_path):
    """Reloading the plugin re-stamps the fingerprint — the banner self-clears rather
    than sticking until restart."""
    src = _checkout(tmp_path)
    link = tmp_path / "plugins" / "project_board"
    link.parent.mkdir(parents=True)
    link.symlink_to(src)
    loader._record_source_fingerprint(_manifest(link))
    (src / "__init__.py").write_text("VALUE = 3\n")
    assert loader.code_drift_warning()

    loader._record_source_fingerprint(_manifest(link))  # what a reload does

    assert loader.code_drift_warning() is None


def test_a_vanished_checkout_does_not_raise(tmp_path):
    """status() must never raise — a deleted checkout reports drift, not a traceback."""
    src = _checkout(tmp_path)
    link = tmp_path / "plugins" / "project_board"
    link.parent.mkdir(parents=True)
    link.symlink_to(src)
    loader._record_source_fingerprint(_manifest(link))

    for f in src.rglob("*.py"):
        f.unlink()

    assert loader.code_drift_warning()  # drifted, not crashed


def test_several_drifted_plugins_are_all_named(tmp_path):
    for pid in ("alpha", "beta"):
        src = _checkout(tmp_path / pid)
        link = tmp_path / pid / "link"
        link.symlink_to(src)
        loader._record_source_fingerprint(_manifest(link, pid))
        (src / "__init__.py").write_text("CHANGED = True\n")

    warning = loader.code_drift_warning()

    assert "alpha" in warning and "beta" in warning
