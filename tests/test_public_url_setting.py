"""``network.public_url`` seeds ``A2A_PUBLIC_URL`` (#4336) — the desktop app can't set env vars."""

import os

import pytest

from graph.config import LangGraphConfig
from graph.settings_schema import is_credential_plane_key, validate_flat
from server.agent_init import _SEEDED_ENV, seed_public_url


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    # setenv first so monkeypatch restores (removes) them — seed writes os.environ directly.
    monkeypatch.setenv("A2A_PUBLIC_URL", "")
    monkeypatch.setenv(_SEEDED_ENV, "")


def _cfg(tmp_path, body: str) -> LangGraphConfig:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(body)
    return LangGraphConfig.from_yaml(str(p))


def test_public_url_is_read_and_trimmed(tmp_path):
    cfg = _cfg(tmp_path, "network:\n  public_url: ' https://box.tail1234.ts.net/ '\n")
    assert cfg.public_url == "https://box.tail1234.ts.net"


def test_public_url_defaults_empty(tmp_path):
    assert _cfg(tmp_path, "network:\n  bind: 127.0.0.1\n").public_url == ""


def test_seed_sets_the_env_when_unset(tmp_path):
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://box.tail1234.ts.net\n"))
    assert os.environ["A2A_PUBLIC_URL"] == "https://box.tail1234.ts.net"


def test_an_explicit_env_value_wins(tmp_path, monkeypatch):
    # A fleet member's A2A_PUBLIC_URL is its hub tenant path; a shared config must not undo it.
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://box.tail1234.ts.net/agents/gymBro-ed9e")
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://box.tail1234.ts.net\n"))
    assert os.environ["A2A_PUBLIC_URL"] == "https://box.tail1234.ts.net/agents/gymBro-ed9e"


def test_empty_setting_leaves_the_env_alone(tmp_path):
    seed_public_url(_cfg(tmp_path, "network:\n  bind: 127.0.0.1\n"))
    assert os.environ["A2A_PUBLIC_URL"] == ""


def test_settings_accepts_the_key():
    ok, err = validate_flat({"network.public_url": "https://box.tail1234.ts.net"})
    assert ok, err


def test_a_changed_setting_applies_across_an_in_app_restart(tmp_path):
    # The restart is an execv that keeps os.environ: the first seed must not read as explicit.
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://a.tail1234.ts.net\n"))
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://b.tail1234.ts.net\n"))
    assert os.environ["A2A_PUBLIC_URL"] == "https://b.tail1234.ts.net"


def test_a_cleared_setting_drops_the_seeded_value_on_restart(tmp_path):
    seed_public_url(_cfg(tmp_path, "network:\n  public_url: https://a.tail1234.ts.net\n"))
    seed_public_url(_cfg(tmp_path, "network:\n  bind: 127.0.0.1\n"))
    assert not os.environ.get("A2A_PUBLIC_URL")


@pytest.mark.parametrize(
    "bad", ["box.tail1234.ts.net", "ftp://box.example", "https://u:p@box.example", "https://box.example/?x=1", "https://box.example?"]
)
def test_a_malformed_url_is_refused(tmp_path, bad):
    ok, err = validate_flat({"network.public_url": bad})
    assert not ok and "network.public_url" in err
    with pytest.raises(ValueError, match="network.public_url"):
        _cfg(tmp_path, f"network:\n  public_url: '{bad}'\n")


def test_a_path_is_allowed(tmp_path):
    cfg = _cfg(tmp_path, "network:\n  public_url: https://edge.example.com/agents/hub/\n")
    assert cfg.public_url == "https://edge.example.com/agents/hub"


def test_a_paired_phone_or_the_agent_cannot_move_it():
    # The card URL is where peers send their bearer: credential plane, like public_console_url.
    assert is_credential_plane_key("network.public_url")
