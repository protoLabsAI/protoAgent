"""Editable launches must work away from the checkout, with stable import names."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform_sensitive
ROOT = Path(__file__).resolve().parents[1]


def test_editable_imports_and_assets_from_a_neutral_cwd(tmp_path):
    # -I removes cwd/PYTHONPATH from import discovery while retaining the editable
    # installation. A pytest pythonpath setting cannot make this child pass.
    code = """
import json
import graph, server, infra.paths, plugins.docs.corpus
print(json.dumps({
    'graph': graph.__file__,
    'server': server.__file__,
    'assets': str(infra.paths._app_root()),
    'docs': str(plugins.docs.corpus.docs_root()),
}))
"""
    run = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert Path(result["graph"]) == ROOT / "src/graph/__init__.py"
    assert Path(result["server"]) == ROOT / "src/server/__init__.py"
    assert Path(result["assets"]) == ROOT
    assert Path(result["docs"]) == ROOT / "docs"


def test_module_cli_from_a_neutral_cwd(tmp_path):
    env = {**os.environ, "PROTOAGENT_BOX_ROOT": str(tmp_path / "box"),
           "PROTOAGENT_HOME": str(tmp_path / "instance")}
    run = subprocess.run([sys.executable, "-I", "-m", "server", "--help"], cwd=tmp_path,
                         env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert "protoagent" in run.stdout.lower()
