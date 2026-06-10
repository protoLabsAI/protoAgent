"""Tests for the egress allowlist + OpenShell policy generator (ADR 0008)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from security import egress


@pytest.fixture(autouse=True)
def _reset():
    egress.set_allowed_hosts([])
    yield
    egress.set_allowed_hosts([])


# ── egress allowlist ───────────────────────────────────────────────────────────


def test_unset_allows_public_blocks_private():
    # No allowlist → public IPs pass, but the default-on SSRF denylist blocks
    # private/loopback/link-local/metadata even without an allowlist. (IP
    # literals so the test doesn't depend on DNS.)
    assert egress.is_enabled() is False
    assert egress.check_url("http://8.8.8.8/x") is None           # public
    for bad in ("http://127.0.0.1/", "http://10.0.0.1/", "http://192.168.1.1/",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/"):
        assert egress.check_url(bad) is not None, bad


def test_allowlisted_host_bypasses_ip_denylist():
    # An operator can intentionally allowlist an internal host — the allowlist is
    # the explicit-trust path and bypasses the private-IP denylist.
    egress.set_allowed_hosts(["internal.svc"])
    assert egress.check_url("http://internal.svc/x") is None
    egress.set_allowed_hosts([])


def test_exact_host_allow_and_deny():
    egress.set_allowed_hosts(["api.proto-labs.ai"])
    assert egress.check_url("https://api.proto-labs.ai/v1/chat") is None
    out = egress.check_url("https://evil.example/exfil")
    assert out and out.startswith("Error:") and "blocked" in out


def test_subdomain_wildcard():
    egress.set_allowed_hosts(["*.proto-labs.ai"])
    assert egress.check_url("https://api.proto-labs.ai/v1") is None   # subdomain
    assert egress.check_url("https://proto-labs.ai/") is None          # apex
    assert egress.check_url("https://api.proto-labs.ai.evil.com/") is not None  # not fooled


def test_case_insensitive_and_port():
    egress.set_allowed_hosts(["API.Example.COM"])
    assert egress.check_url("https://api.example.com:8443/x") is None


def test_malformed_url():
    egress.set_allowed_hosts(["x.com"])
    assert egress.check_url("not a url").startswith("Error:")


def test_set_filters_blanks():
    egress.set_allowed_hosts(["", "  ", "good.com", None])
    assert egress.allowed_hosts() == ["good.com"]


# ── config round-trip ──────────────────────────────────────────────────────────


def test_config_parses_egress(tmp_path):
    from graph.config import LangGraphConfig

    p = tmp_path / "c.yaml"
    p.write_text("egress:\n  allowed_hosts: [api.proto-labs.ai, '*.github.com']\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.egress_allowed_hosts == ["api.proto-labs.ai", "*.github.com"]


def test_config_egress_default_empty():
    from graph.config import LangGraphConfig

    assert LangGraphConfig().egress_allowed_hosts == []


# ── OpenShell policy generator ─────────────────────────────────────────────────


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_openshell_policy", Path(__file__).parent.parent / "scripts" / "gen_openshell_policy.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_policy_reflects_projects_and_egress(tmp_path):
    from graph.config import LangGraphConfig

    p = tmp_path / "c.yaml"
    p.write_text(
        "model:\n  api_base: https://api.proto-labs.ai/v1\n"
        "filesystem:\n"
        "  enabled: true\n"
        "  projects:\n"
        "    - {name: orbis, path: /work/ORBIS, write: false}\n"
        "    - {name: pixelgen, path: /work/pixelgen, write: true}\n"
        "egress:\n  allowed_hosts: ['*.github.com']\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    policy = _gen().build_policy(cfg)
    # filesystem: rw project under read_write, ro project under read_only
    assert "/work/pixelgen" in policy and "/work/ORBIS" in policy
    assert "/sandbox" in policy
    # network: deny-by-default + gateway host + configured host
    assert "default: deny" in policy
    assert "api.proto-labs.ai" in policy
    assert "*.github.com" in policy
    # process + inference domains present
    assert "seccomp: default" in policy
    assert "route_to: https://api.proto-labs.ai/v1" in policy


def test_policy_empty_config_is_default_deny(tmp_path):
    from graph.config import LangGraphConfig

    policy = _gen().build_policy(LangGraphConfig())
    assert "default: deny" in policy
    assert "/sandbox" in policy  # data root always read-write
