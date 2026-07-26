"""requires_pip host-vs-runtime scope (#2246).

The frozen install gate accepted a dep because the MANAGED RUNTIME had it, while the
plugin imported it in the HOST process. The gate's answer was true for the wrong
interpreter: install passed, then every tool call died with ModuleNotFoundError.
"""

from __future__ import annotations

import pytest
import yaml

from graph.plugins import installer
from graph.plugins.manifest import load_manifest


def _plugin(tmp_path, requires_pip, pid="scoped"):
    d = tmp_path / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        yaml.safe_dump({"id": pid, "name": pid, "requires_pip": requires_pip}), encoding="utf-8"
    )
    (d / "__init__.py").write_text("def register(r):\n    pass\n", encoding="utf-8")
    return d


# ── manifest parsing ──────────────────────────────────────────────────────────
def test_bare_string_deps_are_unscoped_and_unchanged(tmp_path):
    m = load_manifest(_plugin(tmp_path, ["httpx>=0.27"]))
    assert m.requires_pip == ["httpx>=0.27"] and m.pip_scopes == {}


def test_scope_host_is_recorded_by_package_name(tmp_path):
    m = load_manifest(_plugin(tmp_path, [{"pkg": "numpy>=2,<3", "scope": "host"}]))
    assert m.pip_scopes == {"numpy": "host"}


def test_scope_runtime_is_the_default_and_not_recorded(tmp_path):
    """Absent ⇒ runtime, so an unscoped manifest parses to an empty mapping and every
    existing plugin behaves exactly as before."""
    m = load_manifest(_plugin(tmp_path, [{"pkg": "pandas", "scope": "runtime"}, "httpx"]))
    assert m.pip_scopes == {}


def test_scope_composes_with_the_optional_tier(tmp_path):
    m = load_manifest(_plugin(tmp_path, [{"pkg": "pillow>=10", "optional": True, "scope": "host"}]))
    assert m.optional_pip == ["pillow>=10"] and m.pip_scopes == {"pillow": "host"}


def test_an_unknown_scope_warns_and_falls_back_rather_than_rejecting(tmp_path, caplog):
    m = load_manifest(_plugin(tmp_path, [{"pkg": "numpy", "scope": "sideways"}]))
    assert m is not None and m.requires_pip == ["numpy"] and m.pip_scopes == {}


# ── the gate ──────────────────────────────────────────────────────────────────
def test_runtime_scoped_dep_is_satisfied_by_the_managed_runtime(monkeypatch):
    """Unchanged ADR 0094 P2 behaviour: a compute plugin's deps live in the child."""
    monkeypatch.setattr(installer, "_importable", lambda n: False)
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: {"pandas"})
    monkeypatch.setattr(installer, "_normalize_dist", lambda n: n)

    assert installer._deps_satisfied(["pandas"]) == (True, [])


def test_host_scoped_dep_is_NOT_satisfied_by_the_managed_runtime(monkeypatch):
    """The bug: the runtime had it, the host imported it, the gate said 'satisfied'."""
    monkeypatch.setattr(installer, "_importable", lambda n: False)
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: {"numpy"})
    monkeypatch.setattr(installer, "_normalize_dist", lambda n: n)

    ok, missing = installer._deps_satisfied(["numpy"], {"numpy": "host"})

    assert ok is False and missing == ["numpy"]


def test_host_scoped_dep_importable_in_the_host_is_satisfied(monkeypatch):
    monkeypatch.setattr(installer, "_importable", lambda n: True)
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())

    assert installer._deps_satisfied(["numpy"], {"numpy": "host"}) == (True, [])


def test_mixed_scopes_are_judged_independently(monkeypatch):
    monkeypatch.setattr(installer, "_importable", lambda n: False)
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: {"pandas", "numpy"})
    monkeypatch.setattr(installer, "_normalize_dist", lambda n: n)

    ok, missing = installer._deps_satisfied(["pandas", "numpy"], {"numpy": "host"})

    assert ok is False and missing == ["numpy"]  # pandas still satisfied by the runtime


# ── the frozen refusal ────────────────────────────────────────────────────────
def test_frozen_install_refuses_a_host_scoped_dep_and_says_why(monkeypatch):
    """It must NOT be pip'd into the managed runtime — that would 'succeed' while the
    in-process import stays broken, which is exactly the failure being fixed."""
    installed: list = []
    monkeypatch.setattr(
        "runtime.python_install.install_requirements_into_managed_runtime",
        lambda specs: installed.append(specs),
    )

    with pytest.raises(installer.InstallError) as exc:
        installer._frozen_install_missing_deps("p", ["numpy"], ["numpy"], {"numpy": "host"})

    assert installed == []  # never sent to the wrong interpreter
    msg = str(exc.value)
    assert "numpy" in msg and "frozen" in msg.lower()
    assert "vendor" in msg.lower() and "scope: runtime" in msg  # names the ways out


def test_frozen_install_still_installs_runtime_scoped_deps(monkeypatch):
    installed: list = []
    monkeypatch.setattr("infra.python_runtime.managed_python_exe", lambda: "/managed/python")
    monkeypatch.setattr(
        "runtime.python_install.install_requirements_into_managed_runtime",
        lambda specs: installed.append(specs),
    )

    installer._frozen_install_missing_deps("p", ["pandas"], ["pandas"], {})

    assert installed == [["pandas"]]
