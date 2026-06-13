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
    # v1 policy schema (validated against OpenShell v0.0.59).
    assert "version: 1" in policy and "filesystem_policy:" in policy
    # filesystem_policy: the write:true project lands under read_write, write:false
    # under read_only — verify by section ORDER, not just presence.
    rw_idx, ro_idx = policy.index("read_write:"), policy.index("read_only:")
    assert rw_idx < policy.index("/work/pixelgen") < ro_idx   # write:true → read_write
    assert ro_idx < policy.index("/work/ORBIS")               # write:false → read_only
    assert "project: pixelgen" in policy and "project: orbis" in policy
    assert "/sandbox" in policy  # data root, read-write
    # network_policies: deny-by-default egress allowlist (only listed endpoints
    # reachable) carrying the gateway host + the configured host.
    assert "network_policies:" in policy and "agent_egress:" in policy
    assert "api.proto-labs.ai" in policy
    assert "*.github.com" in policy
    # process domain: the unprivileged image user (no seccomp / inference domain in v1).
    assert "run_as_user: sandbox" in policy


def test_policy_empty_config_is_default_deny(tmp_path):
    from graph.config import LangGraphConfig

    policy = _gen().build_policy(LangGraphConfig())
    # Deny-by-default egress = a network_policies allowlist: nothing is reachable
    # except the endpoints explicitly listed under agent_egress.
    assert "network_policies:" in policy and "agent_egress:" in policy
    assert "/sandbox" in policy  # data root always read-write


# ── #871: allow_private mode (fleet remotes) ────────────────────────────────────


def test_allow_private_permits_lan_but_blocks_metadata():
    # Fleet remotes are normally LAN / tailnet / loopback — allow those, but ALWAYS
    # block link-local/cloud-metadata, multicast, reserved (the real SSRF targets).
    for ok in ("http://10.0.0.5:7870/", "http://192.168.1.20/", "http://127.0.0.1:7871/",
               "http://100.119.239.8/"):  # tailnet
        assert egress.check_url(ok, allow_private=True) is None, ok
    for bad in ("http://169.254.169.254/latest/meta-data/",  # cloud metadata
                "http://224.0.0.1/",                           # multicast
                "http://0.0.0.0/"):                            # unspecified
        assert egress.check_url(bad, allow_private=True) is not None, bad


def test_default_mode_still_blocks_all_private():
    # The model-probe path keeps the strict default — private + loopback blocked too.
    for bad in ("http://10.0.0.5/", "http://127.0.0.1/", "http://169.254.169.254/"):
        assert egress.check_url(bad) is not None, bad
