import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _isolated_config_home(tmp_path, monkeypatch):
    """Credentials + the thread index live under XDG_CONFIG_HOME: never the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("PROTOAGENT_URL", "PROTOAGENT_TOKEN", "PROTOAGENT_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
