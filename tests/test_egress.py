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
    assert egress.check_url("http://8.8.8.8/x") is None  # public
    for bad in (
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
    ):
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
    assert egress.check_url("https://api.proto-labs.ai/v1") is None  # subdomain
    assert egress.check_url("https://proto-labs.ai/") is None  # apex
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


def test_also_allow_url_includes_gateway_host_when_allowlist_set():
    # When an allowlist IS configured, the operator-configured gateway (api_base) host is
    # always reachable — so the deny-by-default allowlist never blocks the user's own gateway
    # they explicitly pointed the agent at (the "custom baseURL → blocked by egress" report).
    egress.set_allowed_hosts(["api.proto-labs.ai"], also_allow_url="https://my-gateway.internal:4000/v1")
    assert "my-gateway.internal" in egress.allowed_hosts()
    assert egress.check_url("https://my-gateway.internal:4000/v1/models") is None
    # The explicitly-listed host still works; an unrelated host is still denied.
    assert egress.check_url("https://api.proto-labs.ai/v1") is None
    assert egress.check_url("https://evil.example/") is not None


def test_also_allow_url_ignored_when_no_allowlist():
    # Empty allowlist = permissive mode (default SSRF denylist only). Auto-adding the api_base
    # host here would flip the guard into deny-by-default for EVERY other host — so it must not.
    egress.set_allowed_hosts([], also_allow_url="https://my-gateway.internal/v1")
    assert egress.is_enabled() is False
    assert egress.allowed_hosts() == []


def test_also_allow_url_not_duplicated():
    egress.set_allowed_hosts(["gw.example"], also_allow_url="https://gw.example/v1")
    assert egress.allowed_hosts() == ["gw.example"]


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
    # The generator emits each root .resolve()'d — on Windows that anchors "/work/x" to
    # the CWD drive (C:\work\x); resolve the expected values the same way (no-op on POSIX).
    pixelgen, orbis = str(Path("/work/pixelgen").resolve()), str(Path("/work/ORBIS").resolve())
    assert rw_idx < policy.index(pixelgen) < ro_idx  # write:true → read_write
    assert ro_idx < policy.index(orbis)  # write:false → read_only
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


def test_policy_includes_the_default_workspace_fence(tmp_path, monkeypatch):
    """#2281 — the generator read ``filesystem_projects`` RAW, which is empty on a
    default install (the shipped default), so the emitted policy carried no project
    paths at all. Landlock is deny-by-default, so that denied the agent the one
    directory its fs toolset is actually fenced to."""
    from graph.config import LangGraphConfig

    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr("infra.paths.workspace_dir", lambda create=False: ws)

    cfg = LangGraphConfig()  # filesystem enabled, NO explicit projects — the default
    assert cfg.filesystem_projects == []  # the raw field the generator used to read
    policy = _gen().build_policy(cfg)

    rw_idx = policy.index("read_write:")
    assert rw_idx < policy.index(str(ws))  # the workspace is fenced READ-WRITE
    assert "project: workspace" in policy


def test_policy_includes_the_projects_registry(tmp_path):
    """ADR 0095 — a project declared in the ``projects:`` registry reaches the policy
    through the same effective-fence accessor, including its fs:false opt-out."""
    from graph.config import LangGraphConfig

    # Real absolute paths (tmp_path subdirs), NOT "/work/rw": fenced_projects() refuses a
    # non-absolute path, and a POSIX "/work/rw" has a root but no DRIVE on Windows, so
    # WindowsPath("/work/rw").is_absolute() is False — the whole registry would be dropped
    # and the policy would carry no project at all. .as_posix() keeps the YAML drive-colon
    # safe under double quotes on both platforms.
    rw_dir, ro_dir, nf_dir = tmp_path / "rw", tmp_path / "ro", tmp_path / "nofence"
    p = tmp_path / "c.yaml"
    p.write_text(
        "projects:\n"
        f'  - {{name: rw, path: "{rw_dir.as_posix()}"}}\n'  # registry default = read-write (D3)
        f'  - {{name: ro, path: "{ro_dir.as_posix()}", write: false}}\n'
        f'  - {{name: nofence, path: "{nf_dir.as_posix()}", fs: false}}\n'
    )
    policy = _gen().build_policy(LangGraphConfig.from_yaml(p))

    rw_idx, ro_idx = policy.index("read_write:"), policy.index("read_only:")
    # Order by the platform-independent "project: <name>" marker the generator always emits
    # (`f"project: {name}"`) — its resolved path form is drive-anchored on Windows, but the
    # marker is identical on every platform and tests the actual intent (which fence section
    # each project lands in).
    assert rw_idx < policy.index("project: rw") < ro_idx  # registry default = read-write
    assert ro_idx < policy.index("project: ro")  # write:false → read_only
    assert "project: nofence" not in policy  # fs:false grants no filesystem reach


# ── #871: allow_private mode (fleet remotes) ────────────────────────────────────


def test_allow_private_permits_lan_but_blocks_metadata():
    # Fleet remotes are normally LAN / tailnet / loopback — allow those, but ALWAYS
    # block link-local/cloud-metadata, multicast, reserved (the real SSRF targets).
    for ok in (
        "http://10.0.0.5:7870/",
        "http://192.168.1.20/",
        "http://127.0.0.1:7871/",
        "http://100.119.239.8/",
    ):  # tailnet
        assert egress.check_url(ok, allow_private=True) is None, ok
    for bad in (
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://224.0.0.1/",  # multicast
        "http://0.0.0.0/",
    ):  # unspecified
        assert egress.check_url(bad, allow_private=True) is not None, bad


def test_default_mode_still_blocks_all_private():
    # The model-probe path keeps the strict default — private + loopback blocked too.
    for bad in ("http://10.0.0.5/", "http://127.0.0.1/", "http://169.254.169.254/"):
        assert egress.check_url(bad) is not None, bad


def test_policy_expands_tilde_to_match_the_enforced_fence(tmp_path, monkeypatch):
    """The generator emitted the RAW path string while the enforced fence resolves
    every root with Path(...).expanduser() — so a `~/…` entry produced a Landlock
    policy containing a literal "~", which matches nothing. A policy that silently
    disagrees with the fence it describes is worse than no policy."""
    from graph.config import LangGraphConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expands ~ via USERPROFILE, not HOME
    home_dir = tmp_path / "dev" / "proj"
    home_dir.mkdir(parents=True)

    p = tmp_path / "c.yaml"
    p.write_text("projects:\n  - {name: proj, path: ~/dev/proj}\n")
    policy = _gen().build_policy(LangGraphConfig.from_yaml(p))

    assert str(home_dir) in policy
    assert "~/dev/proj" not in policy


def test_policy_never_emits_the_cwd_for_a_blank_path(tmp_path):
    """A whitespace-only path used to become "." — the SERVER's current directory —
    and got emitted as a fenced Landlock root. Both obvious guards miss it: the raw
    value is truthy, and Path("") is Path("."), so the result is truthy too."""
    from graph.config import LangGraphConfig

    p = tmp_path / "c.yaml"
    p.write_text(
        "filesystem:\n"
        "  enabled: true\n"
        "  projects:\n"
        '    - {name: blank, path: "   ", write: true}\n'
        f"    - {{name: real, path: {tmp_path}, write: true}}\n"
    )
    policy = _gen().build_policy(LangGraphConfig.from_yaml(p))

    assert "project: blank" not in policy
    assert "\n    - .\n" not in policy  # never the bare cwd
    assert str(tmp_path) in policy  # the real root still lands


def test_policy_resolves_paths_like_the_enforced_fence(tmp_path):
    """tools/fs_tools.py resolves every root with expanduser().resolve(); a policy
    that normalizes differently describes directories the agent doesn't have."""
    from graph.config import LangGraphConfig

    real = tmp_path / "real"
    real.mkdir()
    p = tmp_path / "c.yaml"
    p.write_text(
        "filesystem:\n"
        "  enabled: true\n"
        "  projects:\n"
        f"    - {{name: dotted, path: {tmp_path}/./real, write: true}}\n"
    )
    policy = _gen().build_policy(LangGraphConfig.from_yaml(p))
    assert str(real.resolve()) in policy
    assert "/./" not in policy
