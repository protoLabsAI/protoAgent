"""A bundled plugin that SUPERSEDES a git-installed copy of the same id.

The mechanism for moving an external plugin (cowork, agent_browser) into core
``plugins/`` WITHOUT renaming it: the bundled manifest lists the retired repo's URL in
``supersedes:``. Without it, every existing install stays on its old git copy forever
(a copy recorded in ``plugins.lock`` wins at any version), Update 400s, auto-update
logs a failure every sweep, uninstall 400s, and archetype bundles that list the old URL
abort — so new agents of that archetype fail to spawn.

Everything here drives the REAL loader + installer against real folders, a real
``plugins.lock`` and real git repos under tmp dirs, in the order it happens in the
field: an "old host" git-installs the plugin, then the "upgraded host" ships a bundled
copy that supersedes it. The only redirections are path seams (the bundled tree and the
loader's roots, which otherwise point at this repo's own ``plugins/``) and the host's
reload capability (``server.agent_init._apply_settings_changes``), which lives above the
layers under test.

Remote URLs: every ``https://git.example.test/<owner>/<repo>`` is served from a local
repo through git's ``url.<base>.insteadOf`` (set via GIT_CONFIG_* env), so the
installer walks its real https path — ls-remote, clone — with no network.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

# Only names that predate the feature are imported at module level, so running this file
# against a host WITHOUT it fails test-by-test (the red check) rather than at collection.
from graph.plugins import installer, loader, setup_gaps
from graph.plugins.loader import discover_plugins, load_plugins
from graph.plugins.manifest import load_manifest

REMOTE = "https://git.example.test"
UPSTREAM = f"{REMOTE}/protoLabsAI/cowork-plugin"  # the retired standalone repo
FORK = f"{REMOTE}/someone/cowork-plugin"  # an operator's deliberate override


def _superseded_gaps() -> list[dict]:
    return [g for g in setup_gaps.active() if g["key"] == loader.SUPERSEDED_GAP_KEY]


def _git(cwd: Path, *args: str) -> None:
    # maintenance.auto=false / gc.auto=0: keep fixture repos inert (#1600).
    subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, msg: str = "init") -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


def _write_plugin(d: Path, pid: str, version: str, *, extra: str = "") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: {version}\n{extra}", encoding="utf-8"
    )
    (d / "__init__.py").write_text("def register(registry):\n    pass\n", encoding="utf-8")
    return d


@pytest.fixture
def host(tmp_path, monkeypatch):
    """An isolated instance (PROTOAGENT_HOME) + an empty bundled tree + a git "remote"."""
    from infra.paths import reset_instance_paths

    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    reset_instance_paths()
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(installer, "bundled_plugins_dir", lambda: bundled)
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remotes.as_uri()}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"{REMOTE}/")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FETCH", "git")
    installer._lsremote_cache.clear()
    installer._lstags_cache.clear()
    setup_gaps.reset()
    ns = types.SimpleNamespace(
        home=home,
        bundled=bundled,
        remotes=remotes,
        live=home / "plugins",
        lock=home / "plugins.lock",
        config=home / "config" / "langgraph-config.yaml",
        secrets=home / "config" / "secrets.yaml",
    )
    yield ns
    installer._lsremote_cache.clear()
    installer._lstags_cache.clear()
    setup_gaps.reset()
    loader.purge_plugin_modules("cowork")


def _remote(host, owner: str, repo: str, pid: str, version: str, *, tags=()) -> str:
    """A plugin repo served at ``https://git.example.test/<owner>/<repo>``."""
    d = _write_plugin(host.remotes / owner / repo, pid, version)
    _git(d, "init", "-q")
    _commit(d)
    for t in tags:
        _git(d, "-c", "user.email=t@t", "-c", "user.name=t", "tag", t)
    return f"{REMOTE}/{owner}/{repo}"


def _release(host, owner: str, repo: str, pid: str, version: str, tag: str) -> None:
    """Cut a newer release on an existing fixture remote."""
    d = _write_plugin(host.remotes / owner / repo, pid, version)
    _commit(d, f"release {tag}")
    _git(d, "-c", "user.email=t@t", "-c", "user.name=t", "tag", tag)


def _ship_bundled(host, pid: str = "cowork", version: str = "0.4.0", supersedes=(UPSTREAM,)) -> Path:
    """The "host upgrade": protoAgent now ships ``pid`` in its own plugins/ tree."""
    extra = "supersedes:\n" + "".join(f"  - {u}\n" for u in supersedes) if supersedes else ""
    return _write_plugin(host.bundled / pid, pid, version, extra=extra)


def _old_host_install(host, url: str = UPSTREAM, ref: str = "v0.3.1") -> dict:
    """What an existing install did BEFORE the plugin was bundled: a normal git install."""
    assert not (host.bundled / "cowork").exists(), "the old host had no bundled copy"
    return installer.install(url, ref)


def _winner(host, superseded=None):
    # `superseded=` only when asked for, so the unchanged-behaviour guards below call the
    # exact signature a host without the feature has (they pass there too, by design).
    kwargs = {} if superseded is None else {"superseded": superseded}
    return {m.id: m for m in discover_plugins([host.bundled, host.live], **kwargs)}


def _write_config(host, doc: dict) -> None:
    host.config.write_text(yaml.safe_dump(doc), encoding="utf-8")


def _read_config(host) -> dict:
    return yaml.safe_load(host.config.read_text(encoding="utf-8")) or {}


# ── URL identity + manifest validation ────────────────────────────────────────────


@pytest.mark.parametrize(
    "spelling",
    [
        "https://github.com/protoLabsAI/cowork-plugin",
        "https://github.com/protoLabsAI/cowork-plugin.git",
        "https://github.com/protoLabsAI/cowork-plugin/",
        "https://github.com/protoLabsAI/cowork-plugin.git/",
        "HTTPS://GitHub.COM/protoLabsAI/Cowork-Plugin",
        "http://github.com/protoLabsAI/cowork-plugin",
        "git@github.com:protoLabsAI/cowork-plugin.git",
        "ssh://git@github.com/protoLabsAI/cowork-plugin.git",
        "https://github.com:443/protoLabsAI/cowork-plugin",
        "https://x-access-token:t0k3n@github.com/protoLabsAI/cowork-plugin",
        "  https://github.com/protoLabsAI/cowork-plugin  ",
    ],
)
def test_canonical_source_equates_every_spelling_of_one_repo(spelling):
    from graph.plugins.manifest import canonical_source

    assert canonical_source(spelling) == "github.com/protolabsai/cowork-plugin"


@pytest.mark.parametrize(
    "other",
    [
        "https://github.com/someone/cowork-plugin",  # a fork — a different repo
        "https://github.com/protoLabsAI/cowork-plugin-evil",  # a name collision, not a path boundary
        "https://gitlab.com/protoLabsAI/cowork-plugin",  # another host
        "https://github.com/protoLabsAI/cowork",
    ],
)
def test_canonical_source_keeps_distinct_repos_apart(other):
    from graph.plugins.manifest import canonical_source

    assert canonical_source(other) != canonical_source("https://github.com/protoLabsAI/cowork-plugin")


@pytest.mark.parametrize("local", ["/Users/me/cowork-plugin", "file:///Users/me/cowork-plugin", "", "C:\\src\\x"])
def test_canonical_source_of_a_local_path_matches_nothing(local):
    from graph.plugins.manifest import canonical_source

    assert canonical_source(local) == ""


def test_manifest_supersedes_keeps_git_urls_and_drops_the_rest(tmp_path, caplog):
    d = _write_plugin(
        tmp_path / "cowork",
        "cowork",
        "0.4.0",
        extra=(
            "supersedes:\n"
            "  - https://github.com/protoLabsAI/cowork-plugin\n"
            "  - git@github.com:protoLabsAI/cowork-plugin.git\n"  # same repo — deduped
            "  - git@github.com:protoLabsAI/cowork-old.git\n"
            "  - file:///Users/me/cowork-plugin\n"
            "  - /Users/me/cowork-plugin\n"
            "  - https://github.com/protoLabsAI/*\n"
            "  - https://github.com\n"
            "  - ftp://example.com/o/r\n"
            "  - 42\n"
        ),
    )
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        m = load_manifest(d)
    assert m.supersedes == [
        "https://github.com/protoLabsAI/cowork-plugin",
        "git@github.com:protoLabsAI/cowork-old.git",
    ]
    for dropped in ("file:///Users/me/cowork-plugin", "/Users/me/cowork-plugin", "protoLabsAI/*", "ftp://", "42"):
        assert dropped in caplog.text


def test_manifest_supersedes_bare_string_is_one_entry_and_absent_is_empty(tmp_path):
    one = _write_plugin(tmp_path / "a", "a", "1.0.0", extra="supersedes: https://github.com/o/a-plugin\n")
    none = _write_plugin(tmp_path / "b", "b", "1.0.0")
    assert load_manifest(one).supersedes == ["https://github.com/o/a-plugin"]
    assert load_manifest(none).supersedes == []


# ── loader: which copy wins ──────────────────────────────────────────────────────


def test_bundled_copy_wins_over_a_superseded_recorded_copy(host):
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "9.9.9", tags=["v0.3.1"])
    _old_host_install(host)  # recorded in plugins.lock from UPSTREAM — even NEWER than bundled
    _ship_bundled(host, version="0.4.0")

    won = _winner(host)["cowork"]
    assert won.path == host.bundled / "cowork" and won.version == "0.4.0"
    # …and the decision is reported, for the loader's operator banner.
    notes: dict = {}
    _winner(host, notes)
    assert notes["cowork"]["source_url"] == UPSTREAM
    assert notes["cowork"]["installed_version"] == "9.9.9"
    assert notes["cowork"]["bundled_version"] == "0.4.0"


@pytest.mark.parametrize(
    "recorded",
    [
        UPSTREAM + ".git",
        UPSTREAM + "/",
        "HTTPS://GIT.EXAMPLE.TEST/protolabsai/COWORK-PLUGIN",
        "git@git.example.test:protoLabsAI/cowork-plugin.git",
        "ssh://git@git.example.test/protoLabsAI/cowork-plugin",
    ],
)
def test_superseded_match_ignores_how_the_install_url_was_spelled(host, recorded):
    _write_plugin(host.live / "cowork", "cowork", "0.3.1")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": recorded}]}))
    _ship_bundled(host)
    assert _winner(host)["cowork"].path == host.bundled / "cowork"


def test_fork_install_still_overrides_the_bundled_copy(host):
    """Guard (unchanged): a copy recorded from any OTHER URL is a deliberate override."""
    _remote(host, "someone", "cowork-plugin", "cowork", "0.2.0", tags=["v0.2.0"])
    installer.install(FORK, "v0.2.0")
    _ship_bundled(host, version="0.4.0")

    assert _winner(host)["cowork"].path == host.live / "cowork"  # older, but recorded + not superseded


def test_unrecorded_copy_rules_are_unchanged(host):
    """Guard (#1574, unchanged): with no lock entry there is no source to supersede — an
    older untracked copy yields to the bundled one, a same-or-newer one still wins."""
    _ship_bundled(host, version="0.4.0")
    _write_plugin(host.live / "cowork", "cowork", "0.3.1")
    assert _winner(host)["cowork"].path == host.bundled / "cowork"
    _write_plugin(host.live / "cowork", "cowork", "0.5.0")
    assert _winner(host)["cowork"].path == host.live / "cowork"


def test_supersedes_on_an_installed_copy_is_inert(host):
    """Guard: the declaration is honored only on the bundled copy. A git-installed copy
    naming its own source can't use it to demote anything."""
    _write_plugin(host.bundled / "cowork", "cowork", "0.4.0")  # bundled, declares nothing
    _write_plugin(host.live / "cowork", "cowork", "0.3.1", extra=f"supersedes: [{UPSTREAM}]\n")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": UPSTREAM}]}))
    assert _winner(host)["cowork"].path == host.live / "cowork"  # tracked override, as before


def test_load_plugins_tells_the_operator_and_the_banner_leaves_with_the_copy(host, monkeypatch):
    from graph.config import LangGraphConfig

    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [host.bundled, host.live])

    res = load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))
    meta = next(m for m in res.meta if m["id"] == "cowork")
    assert meta["loaded"] and meta["version"] == "0.4.0"  # the bundled copy is what runs
    [gap] = _superseded_gaps()
    assert gap["plugin"] == "cowork"
    assert "ships with protoAgent" in gap["message"] and UPSTREAM in gap["message"]
    assert any(w.startswith("cowork plugin: now ships with protoAgent") for w in setup_gaps.warnings())

    # A disabled plugin raises no banner (a disabled plugin's gaps never outlive it).
    load_plugins(LangGraphConfig(plugins_disabled=["cowork"]))
    assert not _superseded_gaps()

    # Enabled again → back; removing the ignored copy clears it at once, and a reload
    # doesn't bring it back.
    load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))
    assert _superseded_gaps()
    installer.uninstall("cowork")
    assert not _superseded_gaps()
    load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))
    assert not _superseded_gaps()


# ── install: skipped, not refused ────────────────────────────────────────────────


def test_install_from_a_superseded_url_fetches_nothing(host):
    # The retired repo doesn't even exist any more (archived / deleted) — no fetch, so
    # it doesn't matter. `force` doesn't turn the skip into an install either.
    _ship_bundled(host)
    for force in (False, True):
        summary = installer.install(UPSTREAM, "v0.3.1", force=force)
        assert summary["superseded"] is True
        assert summary["id"] == "cowork" and summary["version"] == "0.4.0" and summary["resolved_sha"] == ""
    assert not (host.live / "cowork").exists()
    assert installer._read_lock()["plugins"] == []


def test_install_from_a_fork_of_a_bundled_id_is_still_refused(host):
    """Guard (unchanged): the built-in guard still refuses every URL the bundled copy
    doesn't supersede — no silent shadowing."""
    _remote(host, "someone", "cowork-plugin", "cowork", "0.2.0")
    _ship_bundled(host)
    with pytest.raises(installer.InstallError, match="built-in"):
        installer.install(FORK)


def _bundle_repo(host, *, enabled: list[str] | None) -> str:
    """An archetype-style bundle that lists cowork by its OLD URL (like cowork-archetype
    does today), plus a regular git member and a builtin."""
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _remote(host, "protoLabsAI", "google-plugin", "google", "0.5.0", tags=["v0.5.0"])
    repo = host.remotes / "protoLabsAI" / "cowork-archetype"
    repo.mkdir(parents=True)
    doc = {
        "id": "cowork-archetype",
        "name": "Cowork",
        "plugins": [
            {"id": "artifact", "builtin": True},
            {"id": "cowork", "url": UPSTREAM, "ref": "v0.3.1"},
            {"id": "google", "url": f"{REMOTE}/protoLabsAI/google-plugin", "ref": "v0.5.0"},
        ],
    }
    if enabled is not None:
        doc["enabled"] = enabled
    (repo / "protoagent.bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    _git(repo, "init", "-q")
    _commit(repo)
    return f"{REMOTE}/protoLabsAI/cowork-archetype"


def test_bundle_member_listed_by_a_superseded_url_is_skipped_not_fatal(host):
    bundle = _bundle_repo(host, enabled=["artifact", "cowork", "google"])
    _ship_bundled(host)

    summary = installer.install(bundle)
    assert [p["id"] for p in summary["installed"]] == ["google"]  # the rest of the set still lands
    assert summary["skipped_builtin"] == ["artifact"]
    assert summary["skipped_superseded"] == ["cowork"]
    assert summary["enabled"] == ["artifact", "cowork", "google"]
    assert not (host.live / "cowork").exists() and (host.live / "google").exists()
    row = installer.bundle_entry("cowork-archetype")
    assert row["plugins"] == ["google"] and row["superseded"] == ["cowork"]
    assert {e["id"] for e in installer._read_lock()["plugins"]} == {"google"}


async def test_bundle_without_an_enable_list_still_turns_on_the_superseded_member(host):
    """The no-`enabled:` fallback ("turn on every member") turned the member on before it
    moved into core — it must still, on both the live install path and the lock-only
    workspace-create path."""
    from graph.workspaces.manager import _enable_installed_in_config
    from ops import OpContext
    from ops.plugins import install_and_activate

    bundle = _bundle_repo(host, enabled=None)
    _ship_bundled(host)
    cfg = types.SimpleNamespace(plugins_enabled=[], plugins_disabled=[])
    written: dict = {}

    def _apply(updates):
        written.update(updates(cfg) if callable(updates) else updates)
        return True, []

    res = await install_and_activate(
        bundle, ctx=OpContext(knowledge_store=None, graph_config=cfg), apply_settings=_apply
    )
    assert set(written["plugins"]["enabled"]) == {"google", "cowork"}
    assert res.installed_ids == ["google"]  # cowork's code didn't land — the bundled copy runs

    _write_config(host, {"plugins": {"enabled": []}})
    assert set(_enable_installed_in_config(host.config, host.lock)) == {"google", "cowork"}


def test_workspace_created_from_a_superseded_plugin_url_boots_with_it_on(host):
    """The lock-only create/snapshot path enables "what landed" — and a superseded URL
    lands nothing. Given the installed URLs, it enables the bundled copy instead (an old
    host would have fetched + enabled the git copy)."""
    from graph.workspaces.manager import _enable_installed_in_config

    _ship_bundled(host)
    assert installer.install(UPSTREAM)["superseded"] is True  # what the create path's CLI install did
    _write_config(host, {"plugins": {"enabled": ["delegates"]}})
    assert _enable_installed_in_config(host.config, host.lock, sources=[UPSTREAM]) == ["cowork"]
    assert _read_config(host)["plugins"]["enabled"] == ["delegates", "cowork"]


async def test_single_install_of_a_superseded_url_enables_the_bundled_copy(host):
    """Installing the old URL from the console still gets the operator the plugin — the
    bundled one — without claiming any code landed (no stale-router restart flag)."""
    from ops import OpContext
    from ops.plugins import install_and_activate

    _ship_bundled(host)
    cfg = types.SimpleNamespace(plugins_enabled=[], plugins_disabled=[])
    written: dict = {}

    def _apply(updates):
        written.update(updates(cfg) if callable(updates) else updates)
        return True, []

    res = await install_and_activate(UPSTREAM, ctx=OpContext(knowledge_store=None, graph_config=cfg), apply_settings=_apply)
    assert res.enabled == ["cowork"] and written["plugins"]["enabled"] == ["cowork"]
    assert res.installed_ids == []


def test_bundle_update_on_an_upgraded_host_retires_the_old_copy_but_keeps_it_on(host):
    """An existing archetype install (cowork fetched by the old host) is re-resolved on
    the upgraded host: the bundle update no longer aborts, and the member's ignored copy
    is cleaned up without switching the plugin off."""
    bundle = _bundle_repo(host, enabled=["artifact", "cowork", "google"])
    installer.install(bundle)  # old host: cowork fetched as a normal member
    before = list(installer.bundle_entry("cowork-archetype")["plugins"])
    assert "cowork" in before
    _write_config(host, {"plugins": {"enabled": ["artifact", "cowork", "google"]}})
    _ship_bundled(host)  # host upgrade

    installer.install(bundle, force=True, by="cli-update-bundle:cowork-archetype")
    assert installer.orphaned_bundle_members("cowork-archetype", before) == ["cowork"]
    report = installer.uninstall("cowork")
    assert report["superseded_by_bundled"] == "0.4.0"
    assert not (host.live / "cowork").exists()
    assert _read_config(host)["plugins"]["enabled"] == ["artifact", "cowork", "google"]


# ── update / auto-update / sync: skipped with a reason ─────────────────────────────


def _installed_then_superseded(host) -> None:
    """Old host installs cowork@v0.3.1; upstream then cuts v0.3.2; the host upgrades."""
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _release(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.2", "v0.3.2")
    _ship_bundled(host)


def test_update_check_reports_superseded_and_skips_the_network(host):
    _installed_then_superseded(host)
    entry = next(e for e in installer.list_installed() if e["id"] == "cowork")
    assert entry["superseded"] is True and entry["bundled_version"] == "0.4.0"

    status = installer.check_plugin_update(entry)
    assert status["superseded"] is True
    assert status["behind"] is False and status["error"] is None  # no "update available" badge
    assert installer._lstags_cache == {} and installer._lsremote_cache == {}  # never ls-remoted


def _wire_routes(monkeypatch, *, enabled):
    """STATE + a recording stand-in for the host's reload (above the layer under test)."""
    captured: dict = {}
    fake = types.ModuleType("server.agent_init")

    def _apply(config=None, soul=None):
        import runtime.state as _rs

        captured["config"] = config(_rs.STATE.graph_config) if callable(config) else config
        return True, ["reloaded"]

    fake._apply_settings_changes = _apply
    monkeypatch.setitem(sys.modules, "server.agent_init", fake)
    import runtime.state as rs

    cfg = types.SimpleNamespace(plugins_enabled=list(enabled), plugins_disabled=[])
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_meta", [{"id": "cowork", "enabled": True, "views": []}], raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_router_keys", set(), raising=False)
    return captured


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.plugin_routes import register_plugin_routes

    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def test_update_route_explains_instead_of_a_builtin_400(host, monkeypatch):
    _installed_then_superseded(host)
    captured = _wire_routes(monkeypatch, enabled=["cowork"])
    lock_before = host.lock.read_text()

    resp = _client().post("/api/plugins/cowork/update")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "ships with protoAgent" in detail and "uninstall" in detail
    assert host.lock.read_text() == lock_before and "config" not in captured  # nothing pulled, nothing reloaded


async def test_autoupdate_sweep_skips_a_superseded_plugin_without_error_spam(host, monkeypatch, caplog):
    from server import agent_init

    _installed_then_superseded(host)
    lock_before = host.lock.read_text()
    reloads: list = []
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: reloads.append(kw) or (True, []))
    cfg = types.SimpleNamespace(plugins_enabled=["cowork"], plugins_disabled=[], plugins_sources_allow=[])

    with caplog.at_level(logging.INFO):
        updated = await agent_init._plugin_autoupdate_sweep(cfg, {"cowork": {"track": "v0.3.1", "when": "always"}})
    assert updated == 0 and reloads == []
    assert host.lock.read_text() == lock_before
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "autoupdate" in r.getMessage()]
    assert "ships with protoAgent now" in caplog.text


def test_sync_does_not_refetch_a_superseded_copy(host):
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    import shutil

    shutil.rmtree(host.live / "cowork")  # fresh checkout / restored data dir
    assert installer.sync() == [{"id": "cowork", "status": "superseded"}]
    assert not (host.live / "cowork").exists()


# ── uninstall: the ignored copy goes, the plugin stays on ─────────────────────────


def test_uninstall_removes_only_the_ignored_copy_and_keeps_the_plugin_on(host):
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    _write_config(host, {"plugins": {"enabled": ["cowork", "other"], "disabled": []}, "cowork": {"folder": "~/w"}})
    host.secrets.write_text(yaml.safe_dump({"cowork": {"token": "keep-me"}}), encoding="utf-8")

    report = installer.uninstall("cowork", purge=True)  # even purge must not touch the bundled copy's state
    assert report["superseded_by_bundled"] == "0.4.0"
    assert set(report["removed"]) == {"code", "lock"} and report["purged"] is False
    assert not (host.live / "cowork").exists()
    assert installer._read_lock()["plugins"] == []
    cfg = _read_config(host)
    assert cfg["plugins"]["enabled"] == ["cowork", "other"]  # dropping it would switch the bundled copy off
    assert cfg["cowork"] == {"folder": "~/w"}
    assert yaml.safe_load(host.secrets.read_text())["cowork"] == {"token": "keep-me"}
    assert _winner(host)["cowork"].path == host.bundled / "cowork"


def test_uninstall_route_keeps_the_bundled_plugin_enabled_and_skips_the_reload(host, monkeypatch):
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    _write_config(host, {"plugins": {"enabled": ["cowork"]}})
    captured = _wire_routes(monkeypatch, enabled=["cowork"])
    purged: list[str] = []
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: purged.append(pid))

    resp = _client().delete("/api/plugins/cowork")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["superseded_by_bundled"] == "0.4.0"
    assert body["reloaded"] is False and body["restart_recommended"] is False
    assert "config" not in captured and purged == []  # enabled list never rewritten; live modules untouched
    assert _read_config(host)["plugins"]["enabled"] == ["cowork"]
    assert not (host.live / "cowork").exists()


def test_uninstall_of_a_bundled_id_is_still_refused_when_nothing_is_superseded(host):
    """Guard (unchanged): a fork-installed copy of a bundled id isn't retired by
    ``supersedes``, so uninstall keeps refusing the built-in id."""
    _remote(host, "someone", "cowork-plugin", "cowork", "0.2.0")
    installer.install(FORK)
    _ship_bundled(host)
    with pytest.raises(installer.InstallError, match="built-in"):
        installer.uninstall("cowork")
    assert (host.live / "cowork").exists()
