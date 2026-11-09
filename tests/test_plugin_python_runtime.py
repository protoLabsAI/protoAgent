"""Plugins can declare a Python runtime: ``requires_runtimes: {python: {version, requirements}}``
(ADR 0085, amended 2026-11-08; #4807 part 1).

Covers the manifest parse (valid + malformed; node untouched), the per-plugin venv helpers in
``infra.python_runtime`` (where the venv lives, the override env var, the met/unmet state and
provisioning with a STUBBED uv — never a real one), and the loader's ``runtime-missing`` gap
for python (missing / wrong-version / stale → banner with an ``install_python`` action; a
matching venv clears it; loading never provisions).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from graph.plugins import loader, setup_gaps
from graph.plugins.manifest import _parse_requires_runtimes, load_manifest
from infra import python_runtime as pr

# ``infra.python_runtime`` branches on ``os.name`` (venv layout, interpreter path), so the
# Windows CI lane must run this module — mark the whole file platform_sensitive.
pytestmark = pytest.mark.platform_sensitive


# ── helpers ───────────────────────────────────────────────────────────────────


def _py_plugin(tmp_path: Path, version: str = "3.12", reqs=("requests>=2",)):
    """Write an ``atlas`` plugin declaring a python runtime and load its manifest."""
    body = ["requires_runtimes:", "  python:", f'    version: "{version}"']
    if reqs:
        body.append("    requirements:")
        body.extend(f'      - "{r}"' for r in reqs)
    else:
        body.append("    requirements: []")
    d = tmp_path / "atlas"
    d.mkdir(exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        "id: atlas\nname: Atlas\nversion: 0.1.0\n" + "\n".join(body) + "\n", encoding="utf-8"
    )
    return load_manifest(d)


def _write_marker(venv: Path, version: str, requirements) -> None:
    venv.mkdir(parents=True, exist_ok=True)
    (venv / pr._VENV_MARKER).write_text(
        json.dumps({"version": version, "requirements_hash": pr.requirements_hash(requirements)}),
        encoding="utf-8",
    )


class _Proc:
    """A stub ``subprocess.CompletedProcess`` so no test ever shells out to a real uv."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


@pytest.fixture
def venv_override(tmp_path, monkeypatch):
    """Point the ``atlas`` plugin's venv at a tmp dir via the documented override env var, so
    nothing touches the real instance store."""
    venv = tmp_path / "atlas-venv"
    monkeypatch.setenv(pr._venv_env_key("atlas"), str(venv))
    return venv


@pytest.fixture
def gaps():
    setup_gaps.reset()
    yield
    setup_gaps.reset()


def _gap(plugin: str = "atlas"):
    return next((g for g in setup_gaps.active() if g["plugin"] == plugin and g["key"] == loader.RUNTIME_GAP_KEY), None)


# ── manifest parse: python ──────────────────────────────────────────────────────


def test_parse_python_runtime_valid():
    out = _parse_requires_runtimes({"python": {"version": "3.12", "requirements": ["requests>=2", "pandas"]}}, "p")
    assert out == {"python": {"version": "3.12", "requirements": ["requests>=2", "pandas"]}}


def test_parse_python_requirements_default_empty():
    assert _parse_requires_runtimes({"python": {"version": "3.11"}}, "p") == {
        "python": {"version": "3.11", "requirements": []}
    }
    assert _parse_requires_runtimes({"python": {"version": "3.11", "requirements": []}}, "p") == {
        "python": {"version": "3.11", "requirements": []}
    }


def test_parse_node_and_python_coexist():
    out = _parse_requires_runtimes({"node": "20", "python": {"version": "3.12", "requirements": []}}, "p")
    assert out == {"node": ">=20", "python": {"version": "3.12", "requirements": []}}


@pytest.mark.parametrize(
    "spec",
    [
        3.12,  # YAML float, not a mapping
        "3.12",  # not a mapping
        {"version": "3", "requirements": []},  # not N.N
        {"version": "3.12.1", "requirements": []},  # too many parts
        {"version": "3.x", "requirements": []},  # not numeric
        {"requirements": ["x"]},  # no version
        {"version": 312, "requirements": []},  # version not a string (float/int risk)
        {"version": "3.12", "requirements": "requests"},  # requirements not a list
    ],
)
def test_parse_python_drops_malformed(spec, caplog):
    with caplog.at_level(logging.WARNING):
        assert _parse_requires_runtimes({"python": spec}, "p") == {}
    assert "requires_runtimes.python" in caplog.text


def test_parse_python_skips_bad_requirement_members(caplog):
    with caplog.at_level(logging.WARNING):
        out = _parse_requires_runtimes({"python": {"version": "3.12", "requirements": ["ok>=1", 5, "", "  "]}}, "p")
    assert out == {"python": {"version": "3.12", "requirements": ["ok>=1"]}}
    assert "is not a non-empty string" in caplog.text


def test_node_parsing_is_unchanged_alongside_python():
    # The full node suite (tests/test_node_requirement.py) stays green unmodified; this is a
    # local guard that adding python didn't disturb node's scalar-floor parse.
    assert _parse_requires_runtimes({"node": "20"}, "p") == {"node": ">=20"}
    assert _parse_requires_runtimes({"node": ">= 20.6.1"}, "p") == {"node": ">=20.6.1"}


def test_manifest_loads_python_runtime(tmp_path):
    assert _py_plugin(tmp_path).requires_runtimes == {"python": {"version": "3.12", "requirements": ["requests>=2"]}}
    assert _py_plugin(tmp_path, reqs=()).requires_runtimes == {"python": {"version": "3.12", "requirements": []}}


# ── infra.python_runtime: venv location, override, state ─────────────────────────


def test_plugin_venv_dir_honours_the_override(tmp_path, monkeypatch):
    venv = tmp_path / "fixture"
    monkeypatch.setenv(pr._venv_env_key("atlas"), str(venv))
    assert pr.plugin_venv_dir("atlas") == venv


def test_plugin_venv_dir_defaults_under_the_plugin_store(monkeypatch):
    # No override → <plugin_store>/.venv. We don't assert the absolute root (instance-scoped),
    # only the shape: it ends in the plugin id + ".venv".
    monkeypatch.delenv(pr._venv_env_key("atlas"), raising=False)
    venv = pr.plugin_venv_dir("atlas")
    assert venv.name == ".venv" and venv.parent.name == "atlas"


def test_plugin_venv_state(venv_override):
    venv = venv_override
    assert pr.plugin_venv_state("atlas", "3.12", ["requests>=2"]) == "missing"
    _write_marker(venv, "3.12", ["requests>=2"])
    assert pr.plugin_venv_state("atlas", "3.12", ["requests>=2"]) == "ok"
    assert pr.plugin_venv_satisfies("atlas", "3.12", ["requests>=2"]) is True
    assert pr.plugin_venv_state("atlas", "3.11", ["requests>=2"]) == "version"
    assert pr.plugin_venv_state("atlas", "3.12", ["requests>=2", "extra"]) == "stale"


def test_plugin_venv_python_needs_an_interpreter_and_matching_version(venv_override):
    venv = venv_override
    assert pr.plugin_venv_python("atlas") is None  # no interpreter on disk
    exe = pr._venv_python_exe(venv)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    _write_marker(venv, "3.12", [])
    assert pr.plugin_venv_python("atlas") == exe
    assert pr.plugin_venv_python("atlas", "3.12") == exe
    assert pr.plugin_venv_python("atlas", "3.11") is None  # provisioned for a different version


# ── infra.python_runtime: provisioning (uv STUBBED) ──────────────────────────────


def test_provision_runs_uv_and_records_the_marker(venv_override, monkeypatch):
    venv = venv_override
    calls: list[list[str]] = []
    monkeypatch.setattr(pr.shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)
    monkeypatch.setattr(pr.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Proc())

    result = pr.provision_plugin_venv("atlas", "3.12", ["requests>=2"])

    assert result.ok and result.python == pr._venv_python_exe(venv)
    assert calls[0][:3] == ["/opt/uv", "venv", "--python"] and "3.12" in calls[0]
    assert calls[1][:3] == ["/opt/uv", "pip", "install"]
    # The marker it recorded makes the loader see the venv as satisfied.
    assert pr.plugin_venv_state("atlas", "3.12", ["requests>=2"]) == "ok"


def test_provision_skips_pip_when_no_requirements(venv_override, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(pr.shutil, "which", lambda name: "/opt/uv")
    monkeypatch.setattr(pr.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Proc())
    assert pr.provision_plugin_venv("atlas", "3.12", []).ok
    assert len(calls) == 1 and calls[0][1] == "venv"  # no `uv pip install`


def test_provision_reports_missing_uv(venv_override, monkeypatch):
    monkeypatch.setattr(pr.shutil, "which", lambda name: None)
    result = pr.provision_plugin_venv("atlas", "3.12", [])
    assert not result.ok and "uv" in result.error


def test_provision_reports_a_uv_failure(venv_override, monkeypatch):
    monkeypatch.setattr(pr.shutil, "which", lambda name: "/opt/uv")
    monkeypatch.setattr(pr.subprocess, "run", lambda cmd, **kw: _Proc(returncode=1, stderr="boom"))
    result = pr.provision_plugin_venv("atlas", "3.12", [])
    assert not result.ok and "uv venv" in result.error and "boom" in result.error


# ── loader: the runtime-missing gap for python ───────────────────────────────────


def test_missing_venv_raises_the_banner_with_install_action(gaps, venv_override, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "uv_available", lambda: True)
    assert loader._report_runtime_gap(_py_plugin(tmp_path)) == ["python3.12"]
    gap = _gap()
    assert gap is not None and gap["label"] == "Atlas"
    assert "needs a Python 3.12 venv (no venv provisioned)" in gap["message"]
    assert gap["actions"] == [{"kind": "install_python", "target": "atlas", "label": "Provision Python runtime"}]


def test_hash_mismatch_raises_the_banner(gaps, venv_override, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "uv_available", lambda: True)
    _write_marker(venv_override, "3.12", ["a-different-dep"])  # version matches, requirements drifted
    assert loader._report_runtime_gap(_py_plugin(tmp_path)) == ["python3.12"]
    assert "declared dependencies changed" in _gap()["message"]


def test_wrong_version_raises_the_banner(gaps, venv_override, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "uv_available", lambda: True)
    _write_marker(venv_override, "3.11", ["requests>=2"])  # built for the wrong Python
    loader._report_runtime_gap(_py_plugin(tmp_path))
    assert "provisioned for a different Python version" in _gap()["message"]


def test_matching_venv_clears_the_gap(gaps, venv_override, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "uv_available", lambda: True)
    manifest = _py_plugin(tmp_path)
    loader._report_runtime_gap(manifest)
    assert _gap() is not None
    _write_marker(venv_override, "3.12", ["requests>=2"])
    assert loader._report_runtime_gap(manifest) == []
    assert _gap() is None


def test_no_install_action_when_uv_is_absent(gaps, venv_override, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "uv_available", lambda: False)
    loader._report_runtime_gap(_py_plugin(tmp_path))
    gap = _gap()
    assert "Install uv" in gap["message"]
    assert "actions" not in gap


def test_loading_never_provisions(gaps, venv_override, tmp_path, monkeypatch):
    """The load path only ever CHECKS — it must never run uv."""
    monkeypatch.setattr(pr, "uv_available", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("provisioning ran on load")

    monkeypatch.setattr(pr, "provision_plugin_venv", _boom)
    loader._report_runtime_gap(_py_plugin(tmp_path))
    assert _gap() is not None  # gap raised, nothing provisioned


def test_install_action_provisions_and_clears_the_gap(gaps, venv_override, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "uv_available", lambda: True)
    manifest = _py_plugin(tmp_path)
    loader._report_runtime_gap(manifest)
    assert _gap() is not None

    monkeypatch.setattr("graph.plugins.installer.effective_copies", lambda: {"atlas": manifest})
    calls: list[list[str]] = []
    monkeypatch.setattr(pr.shutil, "which", lambda name: "/opt/uv")
    monkeypatch.setattr(pr.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Proc())

    result = loader.install_plugin_python("atlas")
    assert result.ok
    assert any(c[:2] == ["/opt/uv", "venv"] for c in calls)  # the Install action ran uv
    assert _gap() is None  # re-reported as met → banner cleared


def test_install_action_rejects_a_plugin_without_a_python_runtime(monkeypatch, tmp_path):
    node_only = tmp_path / "noderepo"
    node_only.mkdir()
    (node_only / "protoagent.plugin.yaml").write_text(
        'id: atlas\nname: Atlas\nversion: 0.1.0\nrequires_runtimes:\n  node: ">=20"\n', encoding="utf-8"
    )
    manifest = load_manifest(node_only)
    monkeypatch.setattr("graph.plugins.installer.effective_copies", lambda: {"atlas": manifest})
    result = loader.install_plugin_python("atlas")
    assert not result.ok and "no python runtime" in result.error


def test_install_python_action_target_is_forced_to_the_reporting_plugin(gaps):
    setup_gaps.report("atlas", "k", "msg", action={"kind": "install_python", "target": "other", "label": "Go"})
    assert setup_gaps.active()[0]["actions"] == [{"kind": "install_python", "target": "atlas", "label": "Go"}]
