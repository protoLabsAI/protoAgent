"""`sdk.managed_python_exe` — the public seam onto the managed Python runtime (ADR 0094).

Plugins that spawn Python of their own were importing `infra.python_runtime` for this — an
internal with no compatibility promise. The SDK accessor must agree with what
`execute_code` spawns (one notion of the runtime), and it must read a missing or broken
lookup as "not provisioned" rather than raising into a plugin's tool call.
"""

from __future__ import annotations

import pytest

import infra.python_runtime as pr
from graph import sdk
from infra.paths import reset_instance_paths

pytestmark = pytest.mark.platform_sensitive  # the interpreter layout differs on Windows


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A clean box root, so the host's real managed runtime never leaks in."""
    root = tmp_path / "box"
    root.mkdir()
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(root))
    reset_instance_paths()
    yield root
    reset_instance_paths()


def test_none_until_the_runtime_is_provisioned(box):
    assert sdk.managed_python_exe() is None


def test_returns_the_same_interpreter_execute_code_spawns(box):
    exe = pr._python_exe_in(pr.managed_python_install_dir())
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")

    got = sdk.managed_python_exe()

    assert got == exe == pr.managed_python_exe(), "the SDK and execute_code must never disagree"
    assert str(box) in str(got), "the managed runtime is box-scoped"


def test_a_half_extracted_install_reads_as_not_provisioned(box):
    """The install dir exists but the interpreter doesn't — a path that can't spawn is
    worse than None, which lets the caller speak the install step."""
    pr.managed_python_install_dir().mkdir(parents=True)
    assert sdk.managed_python_exe() is None


def test_a_failing_lookup_never_raises_into_the_plugin(monkeypatch):
    def boom():
        raise OSError("box root unreadable")

    monkeypatch.setattr(pr, "managed_python_exe", boom)
    assert sdk.managed_python_exe() is None
