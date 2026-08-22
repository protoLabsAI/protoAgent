"""Workspaces (ADR 0041) — create / list / run / remove."""

from __future__ import annotations

import pytest
import yaml

from graph.workspaces import manager
from tests.privacy_asserts import assert_owner_only


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    # Make port selection machine-independent: treat every port as OS-free, so _pick_port
    # exercises only the registry logic (the OS probe is covered by its own test below —
    # otherwise these assertions depend on whatever's listening on the test host).
    monkeypatch.setattr(manager, "_port_is_free", lambda port: True)
    return tmp_path / "ws"


def test_new_ls_run_rm(root):
    s = manager.create("alpha")
    # The id is opaque + immutable (`alpha-<4hex>`) and keys the dir; the name is display.
    assert s["name"] == "alpha" and s["id"].startswith("alpha-") and s["id"] != "alpha"
    assert s["port"] == 7871
    ws = root / s["id"]
    assert (ws / "config" / "langgraph-config.yaml").exists() and (ws / "workspace.yaml").exists()
    cfg = yaml.safe_load((ws / "config" / "langgraph-config.yaml").read_text())
    assert cfg["instance"]["id"] == s["id"] and cfg["identity"]["name"] == "alpha"

    assert [w["name"] for w in manager.list_workspaces()] == ["alpha"]

    env, argv = manager.run_exec("alpha", [])  # resolves by display name too
    assert env["PROTOAGENT_HOME"] == str(ws)  # <ws> IS the member's instance root
    assert env["PROTOAGENT_INSTANCE"] == s["id"]
    assert "--port" in argv and "7871" in argv

    assert manager.create("beta")["port"] == 7872  # next free port
    with pytest.raises(manager.WorkspaceError):
        manager.create("alpha")  # display-name collision

    # rm keeps the data by default (see TestRemoveKeepsDataUnlessPurged); purge is the
    # irreversible one, and only it deletes the dir.
    assert "workspace" in manager.remove("alpha", purge=True)["removed"] and not ws.exists()


def test_pick_port_skips_os_occupied(root, monkeypatch):
    """_pick_port skips a port held by an UNRELATED process (not just fleet-known ones), so
    a spawned agent doesn't die with EADDRINUSE (the pokemonAgent-on-:7871 collision)."""
    # 7871 is "occupied" by something outside the fleet registry → must be skipped.
    monkeypatch.setattr(manager, "_port_is_free", lambda port: port != 7871)
    assert manager.create("alpha")["port"] == 7872


def test_pick_port_raises_when_range_saturated(root, monkeypatch):
    """A fully-occupied range fails loudly instead of looping forever."""
    monkeypatch.setattr(manager, "_port_is_free", lambda port: False)
    with pytest.raises(manager.WorkspaceError):
        manager.create("alpha")


def test_rename_changes_display_not_id(root):
    s = manager.create("ava")
    out = manager.rename("ava", "nova")
    assert out == {"id": s["id"], "name": "nova"}  # id (slug/data scope) untouched
    ws = manager._find("nova")
    assert ws and ws["id"] == s["id"] and (root / s["id"]).exists()
    cfg = yaml.safe_load((root / s["id"] / "config" / "langgraph-config.yaml").read_text())
    assert cfg["identity"]["name"] == "nova" and cfg["instance"]["id"] == s["id"]
    assert manager._find("nova-x") is None and manager._find(s["id"])["name"] == "nova"

    manager.create("taken")
    with pytest.raises(manager.WorkspaceError):
        manager.rename("nova", "taken")  # display names stay unique
    with pytest.raises(manager.WorkspaceError):
        manager.rename("nova", "host")  # reserved slug


def test_member_self_rename_restamps_its_own_record(root, monkeypatch):
    """A member renaming ITSELF (Settings ▸ Agent ▸ Identity is proxied to the member, so it
    writes the member's identity.name) must restamp its own workspace.yaml — that record is
    what the HUB's fleet list, and so the console's agent switcher/header, displays. Without
    it the tab title and A2A card renamed while the switcher kept the create-time name."""
    s = manager.create("scout")
    ws = root / s["id"]
    # Run as that member: its instance root IS the workspace dir (PROTOAGENT_HOME=<ws>).
    monkeypatch.setattr("infra.paths.instance_paths", lambda: type("P", (), {"instance_root": ws})())

    assert manager.sync_self_display_name("ranger") is None
    rec = yaml.safe_load((ws / "workspace.yaml").read_text())
    assert rec["name"] == "ranger" and rec["label"] == "ranger"
    assert manager._find(s["id"])["label"] == "ranger"  # what the hub lists → the switcher label
    assert manager.sync_self_display_name("ranger") is None  # already in step → no-op

    # A free-form identity.name is kept VERBATIM in `label` (what every surface renders,
    # #2520 — "PA Windows Lifecycle Café" used to silently show as PA_Windows_Lifecycle_Caf);
    # `name` stays the [A-Za-z0-9_-] addressing handle, slugified. No note needed: the
    # display follows exactly, so there is nothing to explain.
    assert manager.sync_self_display_name("PA Windows Lifecycle Café") is None
    rec = yaml.safe_load((ws / "workspace.yaml").read_text())
    assert rec["label"] == "PA Windows Lifecycle Café"
    assert rec["name"] == "PA_Windows_Lifecycle_Caf"
    assert manager._find(s["id"])["label"] == "PA Windows Lifecycle Café"

    # A display with NO addressable characters still saves as the label; the addressing
    # name simply stays put. Only a reserved label is refused (a member masquerading as
    # "host" in the switcher is worse than a stale name) — and never raised: the agent is
    # already running under the new identity, so a stale label must never fail the reload.
    assert manager.sync_self_display_name("!!!") is None
    rec = yaml.safe_load((ws / "workspace.yaml").read_text())
    assert rec["label"] == "!!!" and rec["name"] == "PA_Windows_Lifecycle_Caf"
    assert "reserved" in manager.sync_self_display_name("host")
    assert yaml.safe_load((ws / "workspace.yaml").read_text())["label"] == "!!!"
    # The id (dir, URL slug, data scope) never moves.
    assert yaml.safe_load((ws / "workspace.yaml").read_text())["id"] == s["id"]


@pytest.mark.parametrize(
    "raw,slug",
    [
        ("Merchant Bot", "Merchant_Bot"),
        ("Blood Bowl Coach", "Blood_Bowl_Coach"),
        ("  padded  ", "padded"),
        ("a//b??c", "a_b_c"),  # runs of junk collapse to ONE separator, not one each
        ("--edges__", "edges"),
        ("keep-me_1", "keep-me_1"),  # already legal → untouched
        ("!!!", ""),  # nothing usable survives
        ("", ""),
    ],
)
def test_slugify_display(raw, slug):
    """`identity.name` is free-form; a workspace record's `name` is the [A-Za-z0-9_-]
    addressing handle (the switcher renders the verbatim `label` since #2520). The
    coercion has to be boring and predictable — it's what CLI/control-plane calls accept."""
    assert manager._slugify_display(raw) == slug


def test_sync_self_display_name_noop_on_host(root, tmp_path, monkeypatch):
    """A host/standalone instance root carries no workspace.yaml — nothing to keep in step,
    and the sync must not conjure one (that record is the 'I am a member' signal, #1708)."""
    host_root = tmp_path / "host"
    host_root.mkdir()
    monkeypatch.setattr("infra.paths.instance_paths", lambda: type("P", (), {"instance_root": host_root})())
    assert manager.sync_self_display_name("anything") is None
    assert not (host_root / "workspace.yaml").exists()
    assert manager.is_workspace_member() is False


def test_from_config_clones_and_restamps(root, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "langgraph-config.yaml").write_text(
        "identity: { name: orig }\ninstance: { id: orig }\nmodel: { name: keep-me }\n"
    )
    (src / "secrets.yaml").write_text("model: { api_key: k }\n")
    s = manager.create("clone", from_config=str(src), shared_skills=True)
    cfg = yaml.safe_load((root / s["id"] / "config" / "langgraph-config.yaml").read_text())
    assert cfg["identity"]["name"] == "clone" and cfg["instance"]["id"] == s["id"]
    assert cfg["model"]["name"] == "keep-me"  # other config preserved
    assert cfg["skills"]["shared"] is True
    assert (root / s["id"] / "config" / "secrets.yaml").exists()  # secrets cloned too


def test_bad_name_rejected(root):
    with pytest.raises(manager.WorkspaceError):
        manager.create("bad name")


def test_root_override_wins(root):
    """``PROTOAGENT_WORKSPACES_DIR`` is an explicit override — used verbatim (the
    ``root`` fixture sets it), regardless of instance."""
    assert manager.workspaces_root() == root


def test_root_is_instance_scoped(tmp_path, monkeypatch):
    """ADR 0004: a scoped instance owns its own workspaces root (``instance_root/
    workspaces``, and so its own fleet.json) — two co-located hubs must not share one
    fleet registry. Scope comes from the instance root now, not a scope_leaf knob."""
    import infra.paths as paths

    monkeypatch.delenv("PROTOAGENT_WORKSPACES_DIR", raising=False)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    paths.reset_instance_paths()
    scoped = manager.workspaces_root()
    assert scoped == tmp_path / "roxy" / "workspaces"

    monkeypatch.setenv("PROTOAGENT_INSTANCE", "other")
    paths.reset_instance_paths()
    assert manager.workspaces_root() != scoped  # siblings don't share


def test_fleet_state_follows_scoped_root(tmp_path, monkeypatch):
    """fleet.json lives under the scoped root — a scoped hub's registry is its own."""
    import infra.paths as paths
    from graph.fleet import supervisor

    monkeypatch.delenv("PROTOAGENT_WORKSPACES_DIR", raising=False)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    paths.reset_instance_paths()
    assert supervisor._state_path() == manager.workspaces_root() / "fleet.json"
    assert "roxy" in supervisor._state_path().parts


# ── bundle auto-enable on create (#1346) ──────────────────────────────────────
def _seed_config(ws, enabled=("delegates",)):
    """Write a minimal workspace config with the given plugins.enabled list."""
    ws.mkdir(parents=True, exist_ok=True)
    cfg = ws / "langgraph-config.yaml"
    cfg.write_text(f"plugins:\n  enabled: [{', '.join(enabled)}]\n")
    return cfg


def test_enable_installed_honors_bundle_curated_subset(root):
    """A bundle's curated `enabled` subset is what gets turned on — not every member —
    and `delegates` from the template is preserved."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "plugins": [{"id": "a"}, {"id": "b"}, {"id": "extra"}],
                "bundles": [{"id": "stack", "plugins": ["a", "b", "extra"], "enabled": ["a", "b"]}],
            }
        )
    )
    added = manager._enable_installed_in_config(cfg, ws / "plugins.lock")
    assert added == ["a", "b"]
    enabled = yaml.safe_load(cfg.read_text())["plugins"]["enabled"]
    assert enabled == ["delegates", "a", "b"]  # delegates kept, curated subset added, `extra` left off


def test_enable_installed_falls_back_to_all_members(root):
    """A bundle with no curated `enabled` list enables every installed member."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(
        json.dumps({"plugins": [{"id": "a"}, {"id": "b"}], "bundles": [{"id": "stack", "plugins": ["a", "b"]}]})
    )
    added = manager._enable_installed_in_config(cfg, ws / "plugins.lock")
    assert added == ["a", "b"]
    assert yaml.safe_load(cfg.read_text())["plugins"]["enabled"] == ["delegates", "a", "b"]


def test_enable_installed_bare_plugin_no_bundle(root):
    """A single-plugin install (no bundle record) enables that plugin."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(json.dumps({"plugins": [{"id": "solo"}]}))
    added = manager._enable_installed_in_config(cfg, ws / "plugins.lock")
    assert added == ["solo"]
    assert yaml.safe_load(cfg.read_text())["plugins"]["enabled"] == ["delegates", "solo"]


def test_enable_installed_idempotent_and_missing_lock(root):
    """Already-enabled ids aren't duplicated; a missing lock is a no-op."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws, enabled=("delegates", "a"))
    assert manager._enable_installed_in_config(cfg, ws / "nope.lock") == []  # no lock → no change
    (ws / "plugins.lock").write_text(json.dumps({"bundles": [{"id": "s", "enabled": ["a"]}]}))
    assert manager._enable_installed_in_config(cfg, ws / "plugins.lock") == []  # already on
    assert yaml.safe_load(cfg.read_text())["plugins"]["enabled"] == ["delegates", "a"]


# ── bundle config defaults on create (#1350) ──────────────────────────────────
def test_apply_bundle_config_defaults_seeds_unset_keys(root):
    """A bundle's recommended config defaults land in the workspace config, filling only
    keys the operator hasn't set (a fresh workspace, so everything is unset)."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    cfg.write_text(cfg.read_text() + "agent_browser:\n  panel_mode: compact\n")  # operator pre-set
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "stack",
                        "config": {"agent_browser": {"panel_mode": "full", "timeout": 30}, "board": {"theme": "dark"}},
                    }
                ]
            }
        )
    )
    overlay = manager._apply_bundle_config_defaults(cfg, ws / "plugins.lock")
    assert overlay == {"agent_browser": {"timeout": 30}, "board": {"theme": "dark"}}
    doc = yaml.safe_load(cfg.read_text())
    assert doc["agent_browser"] == {"panel_mode": "compact", "timeout": 30}  # operator value kept, default added
    assert doc["board"] == {"theme": "dark"}  # brand-new section seeded


def test_apply_bundle_config_defaults_noop_without_config(root):
    """A bundle with no `config:` block (or a missing lock) writes nothing."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    before = cfg.read_text()
    assert manager._apply_bundle_config_defaults(cfg, ws / "nope.lock") == {}
    (ws / "plugins.lock").write_text(json.dumps({"bundles": [{"id": "stack", "plugins": ["a"]}]}))
    assert manager._apply_bundle_config_defaults(cfg, ws / "plugins.lock") == {}
    assert cfg.read_text() == before  # untouched


# ── bundle MCP servers on create (ADR 0083 D5, #2011) ─────────────────────────
def test_apply_bundle_mcp_servers_seeds_and_resolves_inputs(root, monkeypatch):
    """A bundle's `mcp:` templates land in `mcp.servers` with `${input}` filled from the
    seed-time env (or a `default`), and `mcp.enabled` flipped on."""
    import json

    monkeypatch.setenv("GITHUB_MCP_TOKEN", "ghp_secret")
    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "stack",
                        "mcp": [
                            {
                                "template": {
                                    "name": "github",
                                    "transport": "http",
                                    "url": "https://api.githubcopilot.com/mcp/",
                                    "headers": {"Authorization": "Bearer ${token}"},
                                },
                                "inputs": [{"key": "token", "env": "GITHUB_MCP_TOKEN", "required": True}],
                            },
                            {
                                "template": {
                                    "name": "fs",
                                    "transport": "stdio",
                                    "command": "npx",
                                    "args": ["-y", "server-filesystem", "${path}"],
                                },
                                "inputs": [{"key": "path", "default": "/tmp/work", "required": True}],
                            },
                        ],
                    }
                ]
            }
        )
    )
    added = manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock")
    assert added == ["github", "fs"]
    doc = yaml.safe_load(cfg.read_text())
    assert doc["mcp"]["enabled"] is True
    by_name = {s["name"]: s for s in doc["mcp"]["servers"]}
    assert by_name["github"]["headers"]["Authorization"] == "Bearer ghp_secret"
    assert by_name["fs"]["args"] == ["-y", "server-filesystem", "/tmp/work"]
    assert "enabled" not in by_name["github"]  # fully resolved → left on


def test_apply_bundle_mcp_servers_disables_unresolved_required(root, monkeypatch):
    """A required `${input}` with no env value or default → the server is seeded but
    `enabled: false` (visible-but-inert), never dropped and never booted half-templated."""
    import json

    monkeypatch.delenv("GITHUB_MCP_TOKEN", raising=False)
    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "stack",
                        "mcp": [
                            {
                                "template": {
                                    "name": "github",
                                    "transport": "http",
                                    "url": "https://api.githubcopilot.com/mcp/",
                                    "headers": {"Authorization": "Bearer ${token}"},
                                },
                                "inputs": [
                                    {"key": "token", "env": "GITHUB_MCP_TOKEN", "required": True, "secret": True}
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    added = manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock")
    assert added == ["github"]
    gh = yaml.safe_load(cfg.read_text())["mcp"]["servers"][0]
    assert gh["enabled"] is False  # inert until the operator supplies the secret
    assert gh["headers"]["Authorization"] == "Bearer "  # placeholder blanked, not left literal


def test_apply_bundle_mcp_servers_unions_by_name_no_clobber(root):
    """A server name already present in the config wins — the bundle never overwrites it."""
    import json

    ws = root / "agent"
    ws.mkdir(parents=True, exist_ok=True)
    cfg = ws / "langgraph-config.yaml"
    cfg.write_text(
        "mcp:\n  enabled: true\n  servers:\n"
        "    - {name: fs, transport: stdio, command: /usr/local/bin/mine}\n"
    )
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "stack",
                        "mcp": [
                            {"template": {"name": "fs", "transport": "stdio", "command": "npx"}},
                            {"template": {"name": "new", "transport": "stdio", "command": "npx"}},
                        ],
                    }
                ]
            }
        )
    )
    added = manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock")
    assert added == ["new"]  # `fs` already present → skipped
    by_name = {s["name"]: s for s in yaml.safe_load(cfg.read_text())["mcp"]["servers"]}
    assert by_name["fs"]["command"] == "/usr/local/bin/mine"  # operator entry untouched
    assert by_name["new"]["command"] == "npx"


def test_apply_bundle_mcp_servers_noop_without_mcp(root):
    """A bundle with no `mcp:` block (or a missing lock) writes nothing."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    before = cfg.read_text()
    assert manager._apply_bundle_mcp_servers(cfg, ws / "nope.lock") == []
    (ws / "plugins.lock").write_text(json.dumps({"bundles": [{"id": "stack", "plugins": ["a"]}]}))
    assert manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock") == []
    assert cfg.read_text() == before  # untouched


# ── operator-supplied MCP inputs on create (#2041) ────────────────────────────
def _gh_mcp_lock():
    """A lock declaring one GitHub MCP template whose ${token} fills from an env var."""
    import json

    return json.dumps(
        {
            "bundles": [
                {
                    "id": "stack",
                    "mcp": [
                        {
                            "template": {
                                "name": "github",
                                "transport": "http",
                                "url": "https://api.githubcopilot.com/mcp/",
                                "headers": {"Authorization": "Bearer ${token}"},
                            },
                            "inputs": [{"key": "token", "env": "GITHUB_MCP_TOKEN", "required": True}],
                        }
                    ],
                }
            ]
        }
    )


def test_apply_bundle_mcp_servers_operator_input_seeds_enabled(root, monkeypatch):
    """An operator-supplied input fills the required ${token} and the server seeds ENABLED —
    even with no env var — replacing the env-only → disabled fallback."""
    monkeypatch.delenv("GITHUB_MCP_TOKEN", raising=False)
    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(_gh_mcp_lock())
    added = manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock", {"token": "ghp_operator"})
    assert added == ["github"]
    gh = yaml.safe_load(cfg.read_text())["mcp"]["servers"][0]
    assert "enabled" not in gh  # required input filled by the operator → left ENABLED
    assert gh["headers"]["Authorization"] == "Bearer ghp_operator"


def test_apply_bundle_mcp_servers_operator_input_beats_env(root, monkeypatch):
    """An operator input wins over the seed-time env var for the same key."""
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "from_env")
    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(_gh_mcp_lock())
    manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock", {"token": "from_operator"})
    gh = yaml.safe_load(cfg.read_text())["mcp"]["servers"][0]
    assert gh["headers"]["Authorization"] == "Bearer from_operator"


def test_apply_bundle_mcp_servers_no_input_still_disables_required(root, monkeypatch):
    """No operator inputs + no env value → the required input stays unresolved and the server
    lands `enabled: false`, exactly as before (#2041 leaves the env-only fallback intact)."""
    monkeypatch.delenv("GITHUB_MCP_TOKEN", raising=False)
    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(_gh_mcp_lock())
    manager._apply_bundle_mcp_servers(cfg, ws / "plugins.lock", {})  # no operator inputs
    gh = yaml.safe_load(cfg.read_text())["mcp"]["servers"][0]
    assert gh["enabled"] is False


# ── operator-supplied secrets on create (#2041) ───────────────────────────────
def _secrets_lock():
    """A lock declaring two bundle secrets under section `devkit`."""
    import json

    return json.dumps(
        {
            "bundles": [
                {
                    "id": "devkit",
                    "secrets": [
                        {"key": "openai_api_key", "label": "OpenAI API Key", "secret": True, "required": True},
                        {"key": "extra_token", "label": "Extra", "secret": True},
                    ],
                }
            ]
        }
    )


def test_apply_bundle_secrets_writes_declared_under_bundle_section(root):
    """Operator values for the bundle's DECLARED secrets land in the member's secrets.yaml
    nested under the bundle's section (its id), 0600, merged with a pre-existing sibling."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "secrets.yaml").write_text("model:\n  api_key: keep-me\n")  # pre-existing sibling secret
    (ws / "plugins.lock").write_text(_secrets_lock())
    written = manager._apply_bundle_secrets(
        cfg,
        ws / "plugins.lock",
        [
            {"key": "openai_api_key", "value": "sk-live"},
            {"key": "undeclared", "value": "nope"},  # not declared by the bundle → ignored
            {"key": "extra_token", "value": ""},  # blank value → ignored
        ],
    )
    assert written == ["openai_api_key"]
    doc = yaml.safe_load((ws / "secrets.yaml").read_text())
    assert doc["devkit"] == {"openai_api_key": "sk-live"}  # nested under the bundle section
    assert doc["model"] == {"api_key": "keep-me"}  # merge-not-clobber
    assert_owner_only(ws / "secrets.yaml")  # owner-only (0o600 POSIX / ACL Windows)


def test_apply_bundle_secrets_never_reads_host_environ(root, monkeypatch):
    """Security: values come from the operator only. A declared secret with a matching HOST env
    var is NOT auto-seeded — only what the operator explicitly passes is written."""
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret-must-not-leak")
    ws = root / "agent"
    cfg = _seed_config(ws)
    import json

    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "devkit",
                        "secrets": [
                            {"key": "openai_api_key", "env": "OPENAI_API_KEY", "required": True},
                            {"key": "other", "required": True},
                        ],
                    }
                ]
            }
        )
    )
    written = manager._apply_bundle_secrets(cfg, ws / "plugins.lock", [{"key": "other", "value": "v"}])
    assert written == ["other"]
    body = (ws / "secrets.yaml").read_text()
    assert yaml.safe_load(body) == {"devkit": {"other": "v"}}  # openai_api_key NOT auto-filled
    assert "host-secret-must-not-leak" not in body


def test_apply_bundle_secrets_noop_paths(root):
    """No operator secrets, a bundle that declares none, or a missing lock all write nothing."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    # empty operator list → early no-op (never touches the lock/file)
    assert manager._apply_bundle_secrets(cfg, ws / "plugins.lock", []) == []
    # missing lock
    assert manager._apply_bundle_secrets(cfg, ws / "nope.lock", [{"key": "x", "value": "y"}]) == []
    # a bundle that declares no secrets
    (ws / "plugins.lock").write_text(json.dumps({"bundles": [{"id": "stack", "plugins": ["a"]}]}))
    assert manager._apply_bundle_secrets(cfg, ws / "plugins.lock", [{"key": "x", "value": "y"}]) == []
    assert not (ws / "secrets.yaml").exists()  # nothing written


# ── bundle config_inputs on create (#2934) ────────────────────────────────────
def _config_inputs_lock(ws):
    """A lock whose bundle declares one prompt of each interesting shape: a required
    string, a boolean with a default, and an optional delegate."""
    import json

    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "stack",
                        "config_inputs": [
                            {"key": "board.repo", "label": "Board repo", "type": "string", "required": True},
                            {"key": "board.auto_merge", "label": "Auto merge", "type": "boolean", "default": False},
                            {"key": "acp.default_delegate", "label": "Delegate", "type": "delegate"},
                        ],
                    }
                ]
            }
        )
    )
    return ws / "plugins.lock"


def test_apply_bundle_config_inputs_writes_declared_dotted_keys(root):
    """Operator answers land at the declared dotted paths (sections created as needed),
    coerced to the declared type; an UNDECLARED key is ignored — the operator can't
    smuggle an arbitrary config path. Unanswered inputs fall back to their default
    (auto_merge) or write nothing (delegate)."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    lock = _config_inputs_lock(ws)
    written = manager._apply_bundle_config_inputs(
        cfg, lock, {"board.repo": "org/repo", "sneaky.path": "x"}
    )
    assert sorted(written) == ["board.auto_merge", "board.repo"]  # answer + defaulted toggle
    doc = yaml.safe_load(cfg.read_text())
    assert doc["board"] == {"repo": "org/repo", "auto_merge": False}
    assert "sneaky" not in doc and "acp" not in doc


def test_apply_bundle_config_inputs_default_never_clobbers_but_answer_wins(root):
    """With no operator answer the declared default fills only an ABSENT key; an
    explicit answer overwrites — the operator just typed it for this install. A
    boolean answer arrives as a wire bool or a form "true"/"false" string alike."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    cfg.write_text(cfg.read_text() + "board:\n  repo: kept/repo\n  auto_merge: true\n")
    lock = _config_inputs_lock(ws)
    assert manager._apply_bundle_config_inputs(cfg, lock, {}) == []  # defaults lose to live values
    assert yaml.safe_load(cfg.read_text())["board"] == {"repo": "kept/repo", "auto_merge": True}
    written = manager._apply_bundle_config_inputs(cfg, lock, {"board.auto_merge": "false"})
    assert written == ["board.auto_merge"]
    assert yaml.safe_load(cfg.read_text())["board"]["auto_merge"] is False


def test_apply_bundle_config_inputs_noop_and_guard_paths(root):
    """Missing lock, no declarations, a blank answer with no default, and a scalar
    sitting mid-path all write nothing — the config is left untouched."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    before = cfg.read_text()
    assert manager._apply_bundle_config_inputs(cfg, ws / "nope.lock", {"a.b": "x"}) == []
    (ws / "plugins.lock").write_text(json.dumps({"bundles": [{"id": "stack", "plugins": ["a"]}]}))
    assert manager._apply_bundle_config_inputs(cfg, ws / "plugins.lock", {"a.b": "x"}) == []
    assert cfg.read_text() == before
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "stack",
                        "config_inputs": [
                            {"key": "board.repo", "label": "Repo", "type": "string"},
                            {"key": "other.key", "label": "Other", "type": "string"},
                        ],
                    }
                ]
            }
        )
    )
    # a scalar where the declared section should be: never clobbered with a dict
    cfg.write_text(before + "board: compact\n")
    assert manager._apply_bundle_config_inputs(cfg, ws / "plugins.lock", {"board.repo": "org/x"}) == []
    assert yaml.safe_load(cfg.read_text())["board"] == "compact"
    # a blank answer with no declared default writes nothing
    assert manager._apply_bundle_config_inputs(cfg, ws / "plugins.lock", {"other.key": "   "}) == []
    assert "other" not in yaml.safe_load(cfg.read_text())


def test_server_argv_frozen_vs_source(monkeypatch):
    """The spawn prefix must adapt to the frozen desktop sidecar: there
    ``sys.executable`` IS the server entrypoint, and a ``-m server`` prefix dies at
    argparse with "unrecognized arguments" — created agents never booted in the
    desktop app (#1565 fallout)."""
    import sys

    assert manager._server_argv() == [sys.executable, "-m", "server"]  # source checkout
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert manager._server_argv() == [sys.executable]


def test_run_exec_frozen_argv(root, monkeypatch):
    """In a frozen build the member launches as ``<sidecar> --port …`` directly."""
    import sys

    manager.create("gamma")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _, argv = manager.run_exec("gamma", ["--ui", "none"])
    assert argv[0] == sys.executable and "-m" not in argv
    assert argv[1] == "--port" and argv[-2:] == ["--ui", "none"]


def test_is_workspace_member_detects_spawned_instance(root, monkeypatch):
    """#1708: a spawned member runs with ``PROTOAGENT_HOME=<ws>`` (run_exec), and the
    workspace registry record at that root is the marker — ``is_workspace_member()``
    is True exactly there, False for a hub/standalone instance root."""
    import infra.paths as paths

    s = manager.create("ava")
    ws = root / s["id"]

    # Standalone/hub: instance root without a workspace.yaml → not a member.
    monkeypatch.setenv("PROTOAGENT_HOME", str(root.parent / "standalone"))
    paths.reset_instance_paths()
    assert manager.is_workspace_member() is False

    # The member's own env (what run_exec wires): instance root IS the workspace dir.
    env, _ = manager.run_exec("ava", [])
    monkeypatch.setenv("PROTOAGENT_HOME", env["PROTOAGENT_HOME"])
    paths.reset_instance_paths()
    assert str(ws) == env["PROTOAGENT_HOME"]
    assert manager.is_workspace_member() is True


class TestRemoveKeepsDataUnlessPurged:
    """#2384 — the console offered an opt-in "Also purge its workspace data (irreversible)"
    switch, but `remove` rmtree'd the workspace either way. For a fleet member that dir IS its
    whole instance root (PROTOAGENT_HOME=<ws>): config, SOUL, chat checkpoints, knowledge,
    inbox, tasks. So leaving the switch OFF destroyed exactly the data it implied you kept."""

    def test_remove_retires_the_agent_and_keeps_every_byte(self, root):
        s = manager.create("ava")
        ws = root / s["id"]
        (ws / "checkpoints.db").write_bytes(b"a whole chat history")

        rep = manager.remove("ava")

        assert rep["removed"] == []  # nothing was deleted, and it says so
        assert ws.exists() and (ws / "checkpoints.db").read_bytes() == b"a whole chat history"
        assert (ws / "config" / "langgraph-config.yaml").exists()
        # Out of the fleet, though — that's what "remove" has to mean.
        assert manager.list_workspaces() == [] and manager._find(s["id"]) is None

    def test_a_retired_agent_can_be_brought_back(self, root):
        """Renaming the record aside IS the whole mechanism, so renaming it back is the whole
        undo — the point of keeping the data is being able to use it again."""
        s = manager.create("ava")
        ws = root / s["id"]
        manager.remove("ava")

        (ws / manager._RETIRED_RECORD).rename(ws / "workspace.yaml")
        back = manager._find(s["id"])
        assert back and back["name"] == "ava" and back["id"] == s["id"]

    def test_the_name_frees_up_so_a_replacement_can_reuse_it(self, root):
        """A retired agent must not keep squatting its display name — 'remove then recreate'
        is the flow this whole fix came out of."""
        first = manager.create("ava")
        manager.remove("ava")

        second = manager.create("ava")  # no collision
        assert second["id"] != first["id"] and (root / first["id"]).exists()

    def test_purge_deletes_the_workspace(self, root):
        s = manager.create("ava")
        assert manager.remove("ava", purge=True)["removed"] == ["workspace"]
        assert not (root / s["id"]).exists()

    def test_purge_resolves_the_legacy_data_scope_through_the_box_root(self, root, tmp_path, monkeypatch):
        """The legacy scope was hard-coded to `~/.protoagent/<id>`, ignoring
        PROTOAGENT_BOX_ROOT — so on the desktop app (box root under ~/Library/Application
        Support) the purge branch could never find anything to delete."""
        box = tmp_path / "box"
        monkeypatch.setattr(
            "infra.paths.instance_paths", lambda: type("P", (), {"box_root": box})()
        )
        s = manager.create("ava")
        legacy = box / s["id"]
        legacy.mkdir(parents=True)
        (legacy / "knowledge.db").write_bytes(b"x")

        assert manager.remove("ava", purge=True)["removed"] == ["workspace", "data"]
        assert not legacy.exists()

    def test_remove_still_rejects_an_unknown_workspace(self, root):
        with pytest.raises(manager.WorkspaceError):
            manager.remove("never-existed")


# ── #2583: a locked workspace is a retryable partial, not a 500 ───────────────


def test_purge_retries_a_transiently_locked_workspace(root, monkeypatch):
    """The Windows race: a member's handles can outlive its process by a moment, so the
    delete right after the stop loses. It must retry rather than fail the whole purge."""
    s = manager.create("alpha")
    ws = root / s["id"]
    calls = {"n": 0}
    real_rmtree = manager.shutil.rmtree

    def flaky(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(32, "The process cannot access the file because it is being used")
        return real_rmtree(path, **kw)

    monkeypatch.setattr(manager.shutil, "rmtree", flaky)

    out = manager.remove("alpha", purge=True)

    assert out["removed"] == ["workspace"] and not ws.exists()
    assert calls["n"] == 2  # first attempt lost the race, second won


def test_purge_reports_a_permanently_locked_workspace_as_busy(root, monkeypatch):
    """When it really won't go, the caller must learn WHICH half happened — the old code let
    the OSError escape and the endpoint answered a generic 500 after already stopping the
    member and clearing its record."""
    manager.create("alpha")

    def always_locked(path, **kw):
        raise OSError(32, "The process cannot access the file because it is being used")

    monkeypatch.setattr(manager.shutil, "rmtree", always_locked)

    with pytest.raises(manager.WorkspaceBusy) as excinfo:
        manager.remove("alpha", purge=True)

    msg = str(excinfo.value)
    assert "IS stopped" in msg and "retry" in msg.lower()  # names the state + the way out
    assert isinstance(excinfo.value, manager.WorkspaceError)  # stays catchable as before


def test_purge_clears_a_read_only_file(root):
    """A read-only file makes rmtree raise on Windows even with nothing holding it. Real
    files, real chmod — no mocking, so this exercises the onexc handler itself."""
    import stat

    s = manager.create("alpha")
    ws = root / s["id"]
    locked = ws / "readonly.txt"
    locked.write_text("x")
    locked.chmod(stat.S_IREAD)

    assert manager.remove("alpha", purge=True)["removed"] == ["workspace"]
    assert not ws.exists()


# ── first-run hardening: required inputs, delegate copy, project registration ──
def _pm_lock(ws, *, project=True):
    """A lock shaped like the Project Manager archetype's Configure step: a required
    path (flagged `project`), a required delegate, an optional string, a toggle."""
    import json

    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "pm",
                        "config_inputs": [
                            {"key": "board.repo", "label": "Repo", "type": "path", "required": True, "project": project},
                            {"key": "board.coder", "label": "Coder delegate", "type": "delegate", "required": True},
                            {"key": "gh.default_repo", "label": "GitHub repo", "type": "string"},
                            {"key": "board.loop_enabled", "label": "Loop", "type": "boolean", "default": False},
                        ],
                    }
                ]
            }
        )
    )
    return ws / "plugins.lock"


def _host_dir(tmp_path):
    """A host config dir with one acp delegate + its per-env secret."""
    host = tmp_path / "host"
    host.mkdir()
    (host / "langgraph-config.yaml").write_text(
        "delegates:\n"
        "  - name: claude-code\n    type: acp\n    command: /abs/claude-agent-acp\n    workdir: /tmp/x\n"
        "  - name: peer\n    type: a2a\n    url: http://peer\n"
    )
    (host / "secrets.yaml").write_text("delegate_secrets:\n  claude-code.env.ANTHROPIC_API_KEY: sk-host\n  peer.auth.token: t\n")
    return host


def test_missing_required_config_inputs_after_apply(root):
    """A required input with no answer, no default, and no live value is reported; an
    answered one, or one the config already carried, is not. Blank answers count as
    missing (the helper never writes them)."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    lock = _pm_lock(ws)
    manager._apply_bundle_config_inputs(cfg, lock, {"board.repo": "  ", "board.coder": None})
    missing = manager.missing_required_config_inputs(cfg, lock)
    assert [m["key"] for m in missing] == ["board.repo", "board.coder"]
    assert missing[1]["label"] == "Coder delegate"
    manager._apply_bundle_config_inputs(cfg, lock, {"board.repo": str(ws), "board.coder": "claude-code"})
    assert manager.missing_required_config_inputs(cfg, lock) == []
    # No lock / no declarations → nothing is required.
    assert manager.missing_required_config_inputs(cfg, ws / "nope.lock") == []


def test_copy_host_delegates_carries_only_the_picked_entry_and_its_secrets(root, tmp_path):
    """The delegate the operator PICKED travels from the host registry into the member's
    own config + secrets overlay; the rest of the host roster does not; an unknown
    name and a missing host dir copy nothing."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    lock = _pm_lock(ws)
    host = _host_dir(tmp_path)
    assert manager.copy_host_delegates(cfg, lock, {"board.coder": "claude-code"}, str(host)) == ["claude-code"]
    doc = yaml.safe_load(cfg.read_text())
    assert [d["name"] for d in doc["delegates"]] == ["claude-code"]
    assert doc["delegates"][0]["command"] == "/abs/claude-agent-acp"
    sec = yaml.safe_load((ws / "secrets.yaml").read_text())
    assert sec["delegate_secrets"] == {"claude-code.env.ANTHROPIC_API_KEY": "sk-host"}
    assert_owner_only(ws / "secrets.yaml")
    # Idempotent: a second copy replaces, never duplicates.
    manager.copy_host_delegates(cfg, lock, {"board.coder": "claude-code"}, str(host))
    assert len(yaml.safe_load(cfg.read_text())["delegates"]) == 1
    assert manager.copy_host_delegates(cfg, lock, {"board.coder": "ghost"}, str(host)) == []
    assert manager.copy_host_delegates(cfg, lock, {"board.coder": "claude-code"}, None) == []


def test_register_project_inputs_registers_checkout_and_scopes_onboarding(root, tmp_path, monkeypatch):
    """A `project: true` path input becomes a `projects:` entry (name = dir name,
    GitHub slug from the origin remote, read-only) and — only when the operator has no
    `onboarding:` section — enables onboarding rooted at the checkout's PARENT. An
    existing entry for the path and an explicit onboarding section are left alone."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    lock = _pm_lock(ws)
    repo = tmp_path / "dev" / "ORBIS"
    repo.mkdir(parents=True)
    monkeypatch.setattr(manager, "github_slug_for_checkout", lambda p: "protoLabsAI/ORBIS" if p == repo else "")
    manager._apply_bundle_config_inputs(cfg, lock, {"board.repo": str(repo), "board.coder": "x"})
    assert manager.register_project_inputs(cfg, lock) == [str(repo)]
    doc = yaml.safe_load(cfg.read_text())
    assert doc["projects"] == [{"name": "ORBIS", "path": str(repo), "github": "protoLabsAI/ORBIS", "write": False}]
    assert doc["onboarding"] == {"enabled": True, "root": str(repo.parent), "allow": ["github.com/protoLabsAI/ORBIS"]}
    # Re-running registers nothing new and never touches onboarding again.
    doc["onboarding"] = {"enabled": False}
    cfg.write_text(yaml.safe_dump(doc))
    assert manager.register_project_inputs(cfg, lock) == []
    assert yaml.safe_load(cfg.read_text())["onboarding"] == {"enabled": False}
    # Not flagged `project` → no registry side effect.
    ws2 = root / "agent2"
    cfg2 = _seed_config(ws2)
    lock2 = _pm_lock(ws2, project=False)
    manager._apply_bundle_config_inputs(cfg2, lock2, {"board.repo": str(repo), "board.coder": "x"})
    assert manager.register_project_inputs(cfg2, lock2) == []
    assert "projects" not in yaml.safe_load(cfg2.read_text())


def test_github_slug_for_checkout_parses_origin(tmp_path):
    """ssh + https remotes parse to owner/name; a non-repo dir yields ''."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:protoLabsAI/ORBIS.git"], check=True)
    assert manager.github_slug_for_checkout(repo) == "protoLabsAI/ORBIS"
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/acme/thing"], check=True)
    assert manager.github_slug_for_checkout(repo) == "acme/thing"
    assert manager.github_slug_for_checkout(tmp_path / "nope") == ""
    # A look-alike host is NOT GitHub.
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://mygithub.com/a/b"], check=True)
    assert manager.github_slug_for_checkout(repo) == ""


def test_overlay_model_copies_secrets_owner_only(root, tmp_path):
    """The inherited host overlay lands 0600 on the member (copyfile drops the mode)."""
    host = tmp_path / "host"
    host.mkdir()
    (host / "langgraph-config.yaml").write_text("model:\n  name: m\n  provider: openai\n")
    (host / "secrets.yaml").write_text("model:\n  api_key: sk-host\n")
    (host / "secrets.yaml").chmod(0o644)
    rec = manager.create("kid", inherit_model=str(host))
    assert_owner_only(root / rec["id"] / "config" / "secrets.yaml")


def test_create_from_bundle_refuses_missing_required_input_and_cleans_up(root, tmp_path, monkeypatch):
    """The create path: a required Configure answer missing → WorkspaceError (→ 400)
    and the half-made workspace is removed, so a retry with the answer succeeds — and
    that retry carries the picked delegate into the member and registers the repo."""
    host = _host_dir(tmp_path)
    repo = tmp_path / "dev" / "proj"
    repo.mkdir(parents=True)
    monkeypatch.setattr(manager, "github_slug_for_checkout", lambda p: "acme/proj")

    def fake_install(ws, bundle):
        _pm_lock(ws)
        return ["board"]

    monkeypatch.setattr(manager, "_install_bundle_into", fake_install)
    with pytest.raises(manager.WorkspaceError, match="Coder delegate \\(board.coder\\)"):
        manager.create("pm", bundle="https://example/pm", config_inputs={"board.repo": str(repo)}, inherit_model=str(host))
    assert not (root / "pm").exists()

    rec = manager.create(
        "pm",
        bundle="https://example/pm",
        config_inputs={"board.repo": str(repo), "board.coder": "claude-code"},
        inherit_model=str(host),
    )
    doc = yaml.safe_load((root / rec["id"] / "config" / "langgraph-config.yaml").read_text())
    assert doc["board"]["repo"] == str(repo) and doc["board"]["coder"] == "claude-code"
    assert [d["name"] for d in doc["delegates"]] == ["claude-code"]
    assert doc["projects"][0]["github"] == "acme/proj"
    assert doc["onboarding"] == {"enabled": True, "root": str(repo.parent), "allow": ["github.com/acme/proj"]}

    # A picked delegate the host does NOT have is refused too (the name alone would ship
    # the pre-fix broken member).
    with pytest.raises(manager.WorkspaceError, match="ghost"):
        manager.create(
            "pm2",
            bundle="https://example/pm",
            config_inputs={"board.repo": str(repo), "board.coder": "ghost"},
            inherit_model=str(host),
        )
    assert not (root / "pm2").exists()


def test_register_project_inputs_no_slug_means_no_onboarding_and_write_default_honoured(root, tmp_path, monkeypatch):
    """Without a parsable GitHub remote, onboarding is NOT enabled (an empty allowlist
    would bind an `onboard_project` that can only refuse); the entry still registers.
    `onboarding.write_default` decides the entry's write flag, like the tool does."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    lock = _pm_lock(ws)
    repo = tmp_path / "local-only"
    repo.mkdir()
    monkeypatch.setattr(manager, "github_slug_for_checkout", lambda p: "")
    manager._apply_bundle_config_inputs(cfg, lock, {"board.repo": str(repo), "board.coder": "x"})
    assert manager.register_project_inputs(cfg, lock) == [str(repo)]
    doc = yaml.safe_load(cfg.read_text())
    assert doc["projects"] == [{"name": "local-only", "path": str(repo), "write": False}]
    assert "onboarding" not in doc
    # write_default flips the registered entry to read-write.
    ws2 = root / "agent2"
    cfg2 = _seed_config(ws2)
    cfg2.write_text(cfg2.read_text() + "onboarding:\n  write_default: true\n")
    lock2 = _pm_lock(ws2)
    manager._apply_bundle_config_inputs(cfg2, lock2, {"board.repo": str(repo), "board.coder": "x"})
    manager.register_project_inputs(cfg2, lock2)
    doc2 = yaml.safe_load(cfg2.read_text())
    assert doc2["projects"][0]["write"] is True
    assert doc2["onboarding"] == {"write_default": True}  # an existing section is never touched


def test_required_gate_edge_cases(root, tmp_path):
    """A required boolean never gates (the toggle always has a value); a relative
    `project` path is reported with the reason; a pending defaults overlay counts."""
    import json

    ws = root / "agent"
    cfg = _seed_config(ws)
    (ws / "plugins.lock").write_text(
        json.dumps(
            {
                "bundles": [
                    {
                        "id": "b",
                        "config_inputs": [
                            {"key": "board.flag", "label": "Flag", "type": "boolean", "required": True},
                            {"key": "board.repo", "label": "Repo", "type": "path", "required": True, "project": True},
                            {"key": "board.coder", "label": "Coder", "type": "delegate", "required": True},
                        ],
                    }
                ]
            }
        )
    )
    lock = ws / "plugins.lock"
    manager._apply_bundle_config_inputs(cfg, lock, {"board.repo": "./rel", "board.coder": ""})
    missing = manager.missing_required_config_inputs(cfg, lock)
    assert [m["key"] for m in missing] == ["board.repo", "board.coder"]
    assert missing[0]["label"] == "Repo — must be an absolute path"
    # The host path passes the bundle's pending `config:` overlay — a key it fills is present.
    missing = manager.missing_required_config_inputs(cfg, lock, {"board": {"coder": "cc"}})
    assert [m["key"] for m in missing] == ["board.repo"]
    # A relative project path is never registered either.
    assert manager.register_project_inputs(cfg, lock) == []


def test_copy_host_delegates_never_swallows_a_sibling_secret(root, tmp_path):
    """`cc` vs `cc.prod`: only `cc.env.*` / `cc.<secret field>` keys travel."""
    ws = root / "agent"
    cfg = _seed_config(ws)
    lock = _pm_lock(ws)
    host = tmp_path / "host"
    host.mkdir()
    (host / "langgraph-config.yaml").write_text(
        "delegates:\n  - name: cc\n    type: acp\n    command: /x\n  - name: cc.prod\n    type: acp\n    command: /y\n"
    )
    (host / "secrets.yaml").write_text(
        "delegate_secrets:\n  cc.env.KEY: sk-cc\n  cc.prod.env.KEY: sk-PROD\n  cc.auth.token: tok\n"
    )
    assert manager.copy_host_delegates(cfg, lock, {"board.coder": "cc"}, str(host)) == ["cc"]
    sec = yaml.safe_load((ws / "secrets.yaml").read_text())
    assert sec["delegate_secrets"] == {"cc.env.KEY": "sk-cc", "cc.auth.token": "tok"}


def test_cli_new_answers_config_inputs_and_copies_the_delegate(root, tmp_path, monkeypatch, capsys):
    """`workspace new --bundle … --input k=v` answers the Configure prompts the console
    would ask; the picked delegate is copied from the HOST config (the CLI runs inside
    the host) without inheriting the host model; a malformed --input is a usage error."""
    from graph import config_io
    from graph.workspaces import cli

    host = _host_dir(tmp_path)
    monkeypatch.setattr(config_io, "config_yaml_path", lambda: host / "langgraph-config.yaml")
    monkeypatch.setattr(manager, "github_slug_for_checkout", lambda p: "")

    def fake_install(ws, bundle):
        _pm_lock(ws)
        return ["board"]

    monkeypatch.setattr(manager, "_install_bundle_into", fake_install)
    repo = tmp_path / "dev" / "r"
    repo.mkdir(parents=True)
    assert cli.run_workspace_cli(["new", "cli-pm", "--bundle", "https://x/pm", "--input", "nonsense"]) == 2
    rc = cli.run_workspace_cli(
        ["new", "cli-pm", "--bundle", "https://x/pm", "--input", f"board.repo={repo}", "--input", "board.coder=claude-code"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "created workspace cli-pm" in out
    ws = next(p for p in root.iterdir() if p.name.startswith("cli-pm"))
    doc = yaml.safe_load((ws / "config" / "langgraph-config.yaml").read_text())
    assert doc["board"] == {"repo": str(repo), "coder": "claude-code", "loop_enabled": False}
    assert [d["name"] for d in doc["delegates"]] == ["claude-code"]
    assert doc["projects"][0]["path"] == str(repo)
