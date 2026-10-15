"""Standalone evals must restore caller roots, including failed initialization."""
import os
from pathlib import Path

import pytest

from evals.isolation import isolated_instance
from infra.paths import instance_paths, reset_instance_paths

pytestmark = pytest.mark.platform_sensitive


@pytest.mark.parametrize("failure", [None, "experiment", "seed"])
def test_isolation_restores_environment_cached_paths_and_removes_temp_root(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "caller"))
    monkeypatch.delenv("PROTOAGENT_BOX_ROOT", raising=False)
    reset_instance_paths()
    caller = instance_paths()
    roots = []
    original_write = Path.write_text

    def seed(path, text, **kwargs):
        roots.append(instance_paths().instance_root)
        if failure == "seed":
            raise RuntimeError("seed failed")
        return original_write(path, text, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", seed)
        try:
            with isolated_instance("pa-isolation-test-", "Synthetic identity") as root:
                assert root == instance_paths().instance_root == instance_paths().box_root
                assert root != caller.instance_root
                assert instance_paths().soul_path.read_text(encoding="utf-8") == "Synthetic identity"
                if failure == "experiment":
                    raise RuntimeError("experiment failed")
        except RuntimeError as exc:
            assert failure and str(exc) == f"{failure} failed"
        else:
            assert failure is None
    assert roots and all(not root.exists() for root in roots)
    assert os.environ["PROTOAGENT_HOME"] == str(caller.instance_root)
    assert "PROTOAGENT_BOX_ROOT" not in os.environ
    assert instance_paths() == caller
