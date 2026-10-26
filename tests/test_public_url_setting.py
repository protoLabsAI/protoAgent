"""``network.public_url`` seeds ``A2A_PUBLIC_URL`` (#4336) — the desktop app can't set env vars."""

import os

from graph.config import LangGraphConfig
from graph.settings_schema import validate_flat
from server.agent_init import seed_public_url


def _cfg(tmp_path, body: str) -> LangGraphConfig:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(body)
    return LangGraphConfig.from_yaml(str(p))


def test_public_url_is_read_and_trimmed(tmp_path):
    cfg = _cfg(tmp_path, "network:\n  public_url: ' https://box.tail1234.ts.net/ '\n")
    assert cfg.public_url == "https://box.tail1234.ts.net"


def test_public_url_defaults_empty(tmp_path):
    assert _cfg(tmp_path, "network:\n  bind: 127.0.0.1\n").public_url == ""


def test_seed_sets_the_env_when_unset(tmp_path, monkeypatch):
    # setenv first so monkeypatch restores (removes) it — seed writes os.environ directly.
    monkeypatch.setenv("A2A_PUBLIC_URL", "")
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://box.tail1234.ts.net\n"))
    assert os.environ["A2A_PUBLIC_URL"] == "https://box.tail1234.ts.net"


def test_an_explicit_env_value_wins(tmp_path, monkeypatch):
    # A fleet member's A2A_PUBLIC_URL is its hub tenant path; a shared config must not undo it.
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://box.tail1234.ts.net/agents/gymBro-ed9e")
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://box.tail1234.ts.net\n"))
    assert os.environ["A2A_PUBLIC_URL"] == "https://box.tail1234.ts.net/agents/gymBro-ed9e"


def test_empty_setting_leaves_the_env_alone(tmp_path, monkeypatch):
    # setenv first so monkeypatch restores (removes) it — seed writes os.environ directly.
    monkeypatch.setenv("A2A_PUBLIC_URL", "")
    seed_public_url(_cfg(tmp_path, "network:\n  bind: 127.0.0.1\n"))
    assert os.environ["A2A_PUBLIC_URL"] == ""


def test_settings_accepts_the_key():
    ok, err = validate_flat({"network.public_url": "https://box.tail1234.ts.net"})
    assert ok, err
