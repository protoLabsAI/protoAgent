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

import asyncio
import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml
from _pytest.outcomes import Failed

# Only names that predate the feature are imported at module level, so running this file
# against a host WITHOUT it fails test-by-test (the red check) rather than at collection.
from graph.plugins import installer, loader, setup_gaps
from graph.plugins.loader import discover_plugins, load_plugins
from graph.plugins.manifest import load_manifest


def _rmtree(path: Path) -> None:
    """Remove a directory that may hold a git CHECKOUT.

    Git marks a clone's pack files read-only, and Windows refuses to delete a read-only
    file — a bare ``shutil.rmtree`` over one dies with ``WinError 5`` (it reddened this
    file's Windows shard). Same handler ``graph/workspaces/manager.py`` uses for the same
    reason: clear the bit, retry the delete.
    """
    import os
    import shutil as _shutil
    import stat

    def _clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        _shutil.rmtree(path, onexc=_clear_readonly)
    else:  # pragma: no cover - the repo pins 3.12+
        _shutil.rmtree(path, onerror=lambda f, t, e: _clear_readonly(f, t, e))

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


def test_supersedes_from_a_sibling_folder_in_the_same_root_is_inert(host):
    """…including a second folder in the SAME root claiming the id: only a copy from an
    EARLIER root (the bundled tree) can retire a later one, so a dropped-in folder can't
    demote the operator's recorded install by declaring its URL."""
    _write_plugin(host.live / "a-cowork", "cowork", "0.1.0", extra=f"supersedes: [{UPSTREAM}]\n")
    _write_plugin(host.live / "b-cowork", "cowork", "0.3.1")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": UPSTREAM}]}))
    notes: dict = {}
    assert _winner(host, notes)["cowork"].path == host.live / "b-cowork"  # the recorded copy, untouched
    assert notes == {}


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
    _rmtree(host.live / "cowork")  # fresh checkout / restored data dir
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


# ═══ Review follow-ups (#3445 adversarial review) ═════════════════════════════════
# Every "which copy of <id> runs?" question has ONE answer (the loader's); removal never
# leaves a loadable leftover; nothing unloads a running bundled copy; and each call site
# the first cut changed is pinned by a test that goes red when that change is reverted.

import importlib.util  # noqa: E402 — grouped with the tests that need it

REPO = Path(__file__).resolve().parents[1]
MODULE = "protoagent_plugin_cowork"


class _Sentinel(types.ModuleType):
    """Stands in for the RUNNING bundled cowork module: a purge pops it from sys.modules."""


def _running_bundled_module(monkeypatch, host) -> types.ModuleType:
    mod = _Sentinel(MODULE)
    mod.__path__ = [str(host.bundled / "cowork")]
    mod.__file__ = str(host.bundled / "cowork" / "__init__.py")
    monkeypatch.setitem(sys.modules, MODULE, mod)
    return mod


def _superseded_pair(host, *, installed_extra: str = "", bundled_extra: str = "", recorded: str = UPSTREAM):
    """An ignored git copy (recorded from `recorded`) + a bundled copy superseding UPSTREAM."""
    _write_plugin(host.live / "cowork", "cowork", "0.3.1", extra=installed_extra)
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": recorded}]}))
    _write_plugin(host.bundled / "cowork", "cowork", "0.4.0", extra=f"supersedes:\n  - {UPSTREAM}\n{bundled_extra}")


# ── one resolver: deps + inventory describe the copy that RUNS ─────────────────────


def test_install_deps_installs_the_running_copys_deps_not_the_ignored_ones(host, monkeypatch):
    _superseded_pair(host, installed_extra="requires_pip:\n  - oldpkg==1.0\n", bundled_extra="requires_pip:\n  - newpkg==2.0\n")
    monkeypatch.setattr(installer, "_frozen_like", lambda: False)
    pip_calls: list[list[str]] = []
    # The pip boundary only — install_deps itself (and its copy choice) runs for real.
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda argv, **kw: pip_calls.append(list(argv)) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert installer.install_deps("cowork") == ["newpkg==2.0"]
    assert [c[-1] for c in pip_calls] == ["newpkg==2.0"]


def test_installed_inventory_describes_the_running_bundled_copy(host, monkeypatch):
    _superseded_pair(host, installed_extra="requires_pip:\n  - oldpkg==1.0\n", bundled_extra="requires_pip:\n  - newpkg==2.0\n")
    _wire_routes(monkeypatch, enabled=["cowork"])
    row = next(r for r in _client().get("/api/plugins/installed").json()["plugins"] if r["id"] == "cowork")
    assert row["superseded"] is True and row["copy_on_disk"] is True
    assert row["manifest"]["version"] == "0.4.0"  # the console joins this onto the running plugin
    assert "oldpkg" not in row["deps_missing"] and "newpkg" in row["deps_missing"]


def test_install_deps_route_asks_no_consent_for_the_bundled_copy(host, monkeypatch):
    # Recorded from an unofficial, un-acked source — but that copy is ignored: the bundled
    # copy runs, nothing was fetched for it, so there is nothing to consent to.
    rando = f"{REMOTE}/rando/cowork-plugin"
    _write_plugin(host.live / "cowork", "cowork", "0.3.1")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": rando}]}))
    _ship_bundled(host, supersedes=(rando,))
    _wire_routes(monkeypatch, enabled=["cowork"])
    body = _client().post("/api/plugins/install-deps", json={"id": "cowork"}).json()
    assert body == {"ok": True, "installed": []}


# ── bundle uninstall / update never unload the running bundled copy ──────────────────


async def test_uninstall_bundle_leaves_the_running_bundled_member_alone(host, monkeypatch):
    from ops import OpContext
    from ops.plugins import uninstall_bundle

    bundle = _bundle_repo(host, enabled=["artifact", "cowork", "google"])
    installer.install(bundle)  # old host: cowork fetched as a member
    _write_config(host, {"plugins": {"enabled": ["artifact", "cowork", "google"]}})
    _ship_bundled(host)  # upgrade
    running = _running_bundled_module(monkeypatch, host)
    reloads: list = []

    rep = await uninstall_bundle(
        "cowork-archetype",
        ctx=OpContext(knowledge_store=None, graph_config=None),
        apply_settings=lambda updates: reloads.append(updates) or (True, []),
    )
    assert rep["removed_members"] == ["google"] and rep["superseded"] == ["cowork"]
    assert len(reloads) == 1  # google really left → one reload; cowork alone would need none
    assert sys.modules.get(MODULE) is running  # the running bundled copy was not purged
    assert "cowork" in _read_config(host)["plugins"]["enabled"]
    assert not (host.live / "cowork").exists()


async def test_update_bundle_retires_the_superseded_member_without_unloading_it(host, monkeypatch):
    from ops import OpContext
    from ops.plugins import update_bundle

    bundle = _bundle_repo(host, enabled=["artifact", "cowork", "google"])
    installer.install(bundle)
    _write_config(host, {"plugins": {"enabled": ["artifact", "cowork", "google"]}})
    _ship_bundled(host)
    running = _running_bundled_module(monkeypatch, host)
    cfg = types.SimpleNamespace(plugins_enabled=["artifact", "cowork", "google"], plugins_disabled=[])

    res = await update_bundle(
        "cowork-archetype",
        ctx=OpContext(knowledge_store=None, graph_config=cfg),
        apply_settings=lambda updates: (True, []),
    )
    assert res.removed_members == ["cowork"] and res.retire_error is None
    assert sys.modules.get(MODULE) is running  # retired copy was the ignored one — nothing unloaded
    assert not (host.live / "cowork").exists()


# ── one lock row per id, for every reader ───────────────────────────────────────────


def test_duplicate_lock_rows_loader_and_uninstall_read_the_same_row(host):
    # [FORK, UPSTREAM]: the last row is UPSTREAM → superseded for the loader AND uninstall.
    _write_plugin(host.live / "cowork", "cowork", "0.3.1")
    host.lock.write_text(
        json.dumps({"plugins": [{"id": "cowork", "source_url": FORK}, {"id": "cowork", "source_url": UPSTREAM}]})
    )
    _ship_bundled(host)
    notes: dict = {}
    _winner(host, notes)
    assert "cowork" in notes
    assert installer.uninstall("cowork")["superseded_by_bundled"] == "0.4.0"
    assert installer._read_lock()["plugins"] == []  # every row for the id went


def test_duplicate_lock_rows_never_delete_a_running_fork_as_superseded(host):
    """Guard: [UPSTREAM, FORK] — the fork (last row) is the recorded copy and runs, so
    uninstall treats it as the fork override it is (refused, as before) — never 'superseded'."""
    _write_plugin(host.live / "cowork", "cowork", "0.2.0")
    host.lock.write_text(
        json.dumps({"plugins": [{"id": "cowork", "source_url": UPSTREAM}, {"id": "cowork", "source_url": FORK}]})
    )
    _ship_bundled(host)
    assert _winner(host)["cowork"].path == host.live / "cowork"
    with pytest.raises(installer.InstallError, match="built-in"):
        installer.uninstall("cowork")
    assert (host.live / "cowork").exists()
    assert _winner(host)["cowork"].path == host.live / "cowork"  # still the operator's override


# ── a superseded row is never "missing on disk" ─────────────────────────────────────


def test_superseded_lock_row_without_files_reads_present_not_missing(host, monkeypatch, capsys):
    from graph.plugins import cli

    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    _rmtree(host.live / "cowork")  # the operator deleted the folder by hand
    [row] = [r for r in installer.list_installed() if r["id"] == "cowork"]
    assert row["superseded"] is True and row["present"] is True and row["copy_on_disk"] is False
    _wire_routes(monkeypatch, enabled=["cowork"])
    assert _client().post("/api/plugins/sync").json()["plugins"] == [{"id": "cowork", "status": "superseded"}]
    monkeypatch.setattr(cli, "_live_servers", lambda: [])
    assert cli.run_plugin_cli(["list"]) == 0
    out = capsys.readouterr().out
    assert "SUPERSEDED" in out and "MISSING" not in out


# ── the bundled copy is found by manifest id, not folder name ─────────────────────────


def test_bundled_folder_named_after_the_repo_is_still_found_by_id(host):
    url = f"{REMOTE}/protoLabsAI/agent-browser-plugin"
    _write_plugin(host.live / "agent_browser", "agent_browser", "0.6.4")
    host.lock.write_text(json.dumps({"plugins": [{"id": "agent_browser", "source_url": url}]}))
    _write_plugin(host.bundled / "agent-browser", "agent_browser", "0.7.0", extra=f"supersedes:\n  - {url}\n")
    _write_config(host, {"plugins": {"enabled": ["agent_browser"]}})
    assert _winner(host)["agent_browser"].path == host.bundled / "agent-browser"
    assert installer.uninstall("agent_browser")["superseded_by_bundled"] == "0.7.0"
    assert _read_config(host)["plugins"]["enabled"] == ["agent_browser"]


def test_every_bundled_plugin_folder_is_named_after_its_manifest_id():
    """The move PR's guard: vendoring `plugins/agent-browser/` for id `agent_browser`
    would work for the loader but read wrong to every folder-keyed tool and human."""
    mismatched = {
        d.name: m.id
        for d in sorted((REPO / "plugins").iterdir())
        if (d / "protoagent.plugin.yaml").is_file() and (m := load_manifest(d)) is not None and m.id != d.name
    }
    assert mismatched == {}


# ── a pin ahead of the bundled version is surfaced, not dropped silently ─────────────


def test_bundle_member_pinned_ahead_of_the_bundled_copy_warns(host):
    bundle = _bundle_repo(host, enabled=["cowork"])
    repo = host.remotes / "protoLabsAI" / "cowork-archetype"
    doc = yaml.safe_load((repo / "protoagent.bundle.yaml").read_text())
    next(p for p in doc["plugins"] if p.get("id") == "cowork")["ref"] = "v0.5.0"
    (repo / "protoagent.bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    _commit(repo, "pin cowork v0.5.0")
    _ship_bundled(host, version="0.4.0")
    summary = installer.install(bundle)
    assert summary["skipped_superseded"] == ["cowork"]
    [warning] = summary["warnings"]
    assert "v0.5.0" in warning and "v0.4.0" in warning


# ── the process still running the removed copy gets it unloaded ──────────────────────


def test_uninstall_route_unloads_a_removed_copy_this_process_was_still_running(host, monkeypatch):
    from graph.config import LangGraphConfig

    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [host.bundled, host.live])
    load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))  # booted on the OLD host: git copy loaded
    assert sys.modules[MODULE].__file__.startswith(str(host.live))
    _ship_bundled(host)  # host upgraded underneath the running process
    captured = _wire_routes(monkeypatch, enabled=["cowork"])

    body = _client().delete("/api/plugins/cowork").json()
    assert body["superseded_by_bundled"] and body["was_loaded"] is True
    assert body["reloaded"] is True and MODULE not in sys.modules  # purged, reloaded onto the bundled copy
    assert captured["config"]["plugins"]["enabled"] == ["cowork"]  # the reload kept it on


# ── removal never leaves a copy that can load ────────────────────────────────────────


def test_symlinked_superseded_copy_is_unlinked_not_left_as_a_loading_bak(host, tmp_path):
    checkout = _write_plugin(tmp_path / "dev" / "cowork-plugin", "cowork", "0.5.0")
    host.live.mkdir(parents=True, exist_ok=True)
    try:
        (host.live / "cowork").symlink_to(checkout, target_is_directory=True)  # #2298 dev workflow
    except OSError:
        pytest.skip("this platform can't create directory symlinks here")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": UPSTREAM}]}))
    _ship_bundled(host, version="0.4.0")
    _write_config(host, {"plugins": {"enabled": ["cowork"]}})

    assert installer.uninstall("cowork")["superseded_by_bundled"]
    assert checkout.exists() and (checkout / "protoagent.plugin.yaml").exists()  # the dev's checkout is untouched
    assert sorted(p.name for p in host.live.iterdir()) == []  # no `cowork.bak`
    assert _winner(host)["cowork"].path == host.bundled / "cowork"


def test_a_swap_leftover_bak_never_loads_or_lists(host):
    _ship_bundled(host, version="0.4.0")
    _write_plugin(host.live / "cowork.bak", "cowork", "0.9.9")  # an interrupted swap's set-aside copy
    assert _winner(host)["cowork"].path == host.bundled / "cowork"  # untracked + newer, yet inert
    assert [r["id"] for r in installer.list_installed()] == []


# ── the matcher is a real URL parser; secrets never reach the banner or logs ──────────


@pytest.mark.parametrize(
    "trick",
    [
        "https://evil.example#@github.com/protoLabsAI/cowork-plugin",
        "https://evil.example?@github.com/protoLabsAI/cowork-plugin",
        "https://evil.example\\@github.com/protoLabsAI/cowork-plugin",
    ],
)
def test_canonical_source_is_not_fooled_by_at_sign_tricks(trick):
    from graph.plugins.manifest import canonical_source

    assert canonical_source(trick) != "github.com/protolabsai/cowork-plugin"


@pytest.mark.parametrize(
    "spelling",
    [
        "https://github.com/protoLabsAI/cowork-plugin?ref=main",
        "https://github.com/protoLabsAI/cowork-plugin#readme",
        "https://www.github.com/protoLabsAI/cowork-plugin",
        "git@github.com:protoLabsAI/cowork-plugin.git#main",
    ],
)
def test_canonical_source_ignores_query_fragment_and_www(spelling):
    from graph.plugins.manifest import canonical_source

    assert canonical_source(spelling) == "github.com/protolabsai/cowork-plugin"


def test_a_token_in_the_install_url_never_reaches_the_banner_logs_or_409(host, monkeypatch, caplog):
    from graph.config import LangGraphConfig

    secret_url = "https://x-access-token:SEKRET@git.example.test/protoLabsAI/cowork-plugin?token=SEKRET2"
    _superseded_pair(host, recorded=secret_url)
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [host.bundled, host.live])
    with caplog.at_level(logging.INFO):
        load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))
        _wire_routes(monkeypatch, enabled=["cowork"])
        detail = _client().post("/api/plugins/cowork/update").json()["detail"]
    [gap] = _superseded_gaps()
    for text in (gap["message"], caplog.text, detail):
        assert "SEKRET" not in text
    assert "git.example.test/protoLabsAI/cowork-plugin" in gap["message"]


def test_a_superseded_copy_not_older_than_the_bundled_one_is_called_out(host, monkeypatch, caplog):
    from graph.config import LangGraphConfig

    _write_plugin(host.live / "cowork", "cowork", "9.9.9")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": UPSTREAM}]}))
    _ship_bundled(host, version="0.4.0")
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [host.bundled, host.live])
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))
    assert "is not older than the bundled one" in caplog.text


def test_sync_handles_a_lock_row_with_no_source_url(host):
    # ADR 0093 wheel-deps pins a BUNDLED plugin gets: a lock row with deps and no source.
    _ship_bundled(host)
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "deps": []}, {"id": "ghost", "deps": []}]}))
    results = {r["id"]: r for r in installer.sync()}
    assert results["cowork"] == {"id": "cowork", "status": "present"}
    assert results["ghost"]["status"] == "failed" and "no source_url" in results["ghost"]["error"]


# ── call sites the first cut changed, end to end ─────────────────────────────────────


def _cli_in_a_child_process(host, monkeypatch) -> None:
    """Make the lock-only create/snapshot paths' `python -m server plugin install` child
    see THIS test's bundled tree: the child runs the real CLI + installer, with only the
    bundled-tree path pointed at the fixture (the same seam the in-process tests use)."""
    from graph.workspaces import manager

    shim = (
        "import sys\n"
        "from pathlib import Path\n"
        "from graph.plugins import installer\n"
        f"installer.bundled_plugins_dir = lambda: Path({str(host.bundled)!r})\n"
        "from graph.plugins.cli import run_plugin_cli\n"
        "sys.exit(run_plugin_cli(sys.argv[2:]))\n"
    )
    monkeypatch.setattr(manager, "_server_argv", lambda: [sys.executable, "-c", shim])
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(host.home.parent / "child-box"))


def test_snapshot_import_turns_on_a_pin_whose_url_is_now_superseded(host, monkeypatch):
    from graph.snapshot_import import PluginPin, _install_pins

    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _ship_bundled(host)
    _cli_in_a_child_process(host, monkeypatch)
    ws = host.home.parent / "imported-agent"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "langgraph-config.yaml").write_text("plugins:\n  enabled: []\n")

    installed, failed = _install_pins(ws, [PluginPin(id="cowork", url=UPSTREAM, ref="v0.3.1")])
    assert (installed, failed) == (["cowork"], [])
    assert not (ws / "plugins" / "cowork").exists()  # nothing fetched — the bundled copy is the plugin
    cfg = yaml.safe_load((ws / "config" / "langgraph-config.yaml").read_text())
    assert cfg["plugins"]["enabled"] == ["cowork"]


def test_workspace_created_from_a_superseded_plugin_url_boots_with_it_enabled(host, monkeypatch):
    from graph.workspaces import manager

    _ship_bundled(host)
    _cli_in_a_child_process(host, monkeypatch)
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(host.home.parent / "workspaces"))
    monkeypatch.setattr(manager, "_port_is_free", lambda port: True)

    rec = manager.create("coworker", bundle=UPSTREAM)
    cfg = yaml.safe_load((Path(rec["path"]) / "config" / "langgraph-config.yaml").read_text())
    assert "cowork" in cfg["plugins"]["enabled"]


def test_archetype_preview_describes_the_bundled_copy_without_fetching(host):
    from ops import plugins as ops_plugins

    bundle = _bundle_repo(host, enabled=["cowork", "google"])
    _ship_bundled(host)
    _rmtree(host.remotes / "protoLabsAI" / "cowork-plugin")  # the retired repo is gone
    ops_plugins._peek_cache.clear()
    try:
        preview = ops_plugins._peek_bundle_sync(bundle)
    finally:
        ops_plugins._peek_cache.clear()
    member = next(m for m in preview["members"] if m["id"] == "cowork")
    assert member.get("superseded") is True and member["version"] == "0.4.0" and "error" not in member


def _devkit(monkeypatch):
    spec = importlib.util.spec_from_file_location("pdk_supersedes_test", REPO / "plugins" / "plugin-devkit" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    applied: list = []
    monkeypatch.setattr(mod, "_live_apply", lambda updates: applied.append(updates) or (True, "applied"))
    return mod, applied


async def test_devkit_update_tool_explains_instead_of_pulling(host, monkeypatch):
    mod, applied = _devkit(monkeypatch)
    _installed_then_superseded(host)
    lock_before = host.lock.read_text()
    out = await mod.update_plugin.ainvoke({"plugin_id": "cowork"})
    assert out.startswith("✗ nothing to update") and "ships with protoAgent" in out
    assert host.lock.read_text() == lock_before and applied == []


async def test_devkit_uninstall_tool_leaves_the_running_bundled_copy_alone(host, monkeypatch):
    mod, applied = _devkit(monkeypatch)
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    running = _running_bundled_module(monkeypatch, host)
    out = await mod.uninstall_plugin.ainvoke({"plugin_id": "cowork"})
    assert "removed the superseded copy" in out and "keeps running" in out
    assert applied == [] and sys.modules.get(MODULE) is running
    assert not (host.live / "cowork").exists()


def test_cli_reports_every_superseded_outcome(host, monkeypatch, capsys):
    from graph.plugins import cli

    monkeypatch.setattr(cli, "_live_servers", lambda: [{"pid": 7, "port": 7870}])
    _ship_bundled(host)
    assert cli.run_plugin_cli(["install", UPSTREAM]) == 0
    assert "ships with protoAgent now (bundled v0.4.0) — nothing fetched" in capsys.readouterr().out

    bundle = _bundle_repo(host, enabled=["cowork", "google"])
    assert cli.run_plugin_cli(["install", bundle]) == 0
    assert "moved into protoAgent" in capsys.readouterr().out

    _rmtree(host.bundled / "cowork")  # an OLD host installs the member for real…
    installer.install(UPSTREAM, "v0.3.1")
    _ship_bundled(host)  # …then upgrades
    assert cli.run_plugin_cli(["uninstall", "cowork"]) == 0
    out = capsys.readouterr().out
    assert "that was the superseded copy" in out and "may still be running the removed copy" in out


# ═══ Round-2 review follow-ups ════════════════════════════════════════════════════
# The consent/allowlist waiver is the security-relevant branch of the resolver, so both
# directions are pinned: GIVEN for a copy that really is the bundled one, WITHHELD for
# anything else — including the two ways the live root and the bundled tree can coincide.


def _pip_recorder(monkeypatch) -> list[list[str]]:
    monkeypatch.setattr(installer, "_frozen_like", lambda: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda argv, **kw: calls.append(list(argv)) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    return calls


def test_waiver_is_refused_for_a_fork_override(host):
    """A tracked fork copy RUNS, so its origin still gates deps (the #2743 re-check)."""
    _write_plugin(host.live / "cowork", "cowork", "0.9.0")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": FORK}]}))
    _ship_bundled(host)
    _write_config(host, {"plugins": {"sources": {"allow": []}}})  # deny-all
    assert installer.effective_source_url("cowork") == FORK
    with pytest.raises(installer.InstallError, match="no longer on"):
        installer.install_deps("cowork")


def test_waiver_is_refused_for_a_live_copy_symlinked_into_the_bundled_tree(host):
    """A live-dir entry POINTING at a bundled folder is still an installed copy."""
    _ship_bundled(host, version="0.4.0")
    host.live.mkdir(parents=True, exist_ok=True)
    try:
        (host.live / "other").symlink_to(host.bundled / "cowork", target_is_directory=True)
    except OSError:
        pytest.skip("this platform can't create directory symlinks here")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": FORK}]}))
    assert _winner(host)["cowork"].path == host.live / "other"  # tracked override
    assert installer.effective_source_url("cowork") == FORK


def test_waiver_follows_the_configured_plugins_dir(host):
    """``plugins.dir`` moves the loader's live root — so the resolver has to read it too,
    or it answers about a folder the loader never looks at."""
    alt = host.home / "alt-plugins"
    _write_plugin(alt / "cowork", "cowork", "0.9.0")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": FORK}]}))
    _ship_bundled(host)
    _write_config(host, {"plugins": {"dir": str(alt), "sources": {"allow": []}}})
    assert {m.id: m.path for m in discover_plugins([host.bundled, alt])}["cowork"] == alt / "cowork"
    assert installer.effective_copies()["cowork"].path == alt / "cowork"
    assert installer.effective_source_url("cowork") == FORK
    with pytest.raises(installer.InstallError, match="no longer on"):
        installer.install_deps("cowork")


def test_waiver_is_refused_when_the_live_dir_is_the_bundled_dir(host, monkeypatch):
    """``PROTOAGENT_PLUGINS_DIR`` aimed at the app's own plugins tree: every installed
    copy then sits in the bundled tree, so "is it the bundled copy?" can't be a parent-dir
    comparison alone."""
    monkeypatch.setattr(installer, "live_plugins_dir", lambda: host.bundled)
    _write_plugin(host.bundled / "randoplug", "randoplug", "1.0.0")
    rando = f"{REMOTE}/rando/randoplug"
    host.lock.write_text(json.dumps({"plugins": [{"id": "randoplug", "source_url": rando}]}))
    _write_config(host, {"plugins": {"sources": {"allow": []}}})  # deny-all
    assert installer.effective_source_url("randoplug") == rando
    with pytest.raises(installer.InstallError, match="no longer on"):
        installer.install_deps("randoplug")


def test_waived_deps_are_always_the_bundled_manifests(host, monkeypatch):
    """The security shape when the gate IS waived: the pip list can only be the bundled
    manifest's — the ignored copy's deps are never what an unchecked install installs."""
    _superseded_pair(
        host,
        installed_extra="requires_pip:\n  - oldpkg==1.0\n",
        bundled_extra="requires_pip:\n  - newpkg==2.0\n",
    )
    _write_config(host, {"plugins": {"sources": {"allow": []}}})  # deny-all
    calls = _pip_recorder(monkeypatch)
    assert installer.effective_source_url("cowork") == ""
    assert installer.install_deps("cowork") == ["newpkg==2.0"]
    assert [c[-1] for c in calls] == ["newpkg==2.0"]


def test_a_bundled_plugins_deps_pin_row_is_not_reported_missing(host):
    """A wheel-deps pin (ADR 0093) for a BUNDLED plugin is a lock row with no source_url
    and no folder. ``sync`` says "present"; the inventory has to agree, or the console's
    "in plugins.lock but missing on disk — Sync plugins" alert can never clear."""
    _write_plugin(host.bundled / "cowork", "cowork", "0.4.0")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "deps": [{"name": "pypdf", "version": "1.0"}]}]}))
    assert installer.sync() == [{"id": "cowork", "status": "present"}]
    [row] = [r for r in installer.list_installed() if r["id"] == "cowork"]
    assert row["present"] is True and row["copy_on_disk"] is False


def test_check_updates_returns_one_row_per_id(host):
    """Every plugin-level reader picks one lock row per id — the update check included, or
    a duplicated lock reports an update from the row the loader doesn't use."""
    _write_plugin(host.live / "cowork", "cowork", "0.3.1")
    host.lock.write_text(
        json.dumps({"plugins": [{"id": "cowork", "source_url": FORK}, {"id": "cowork", "source_url": UPSTREAM}]})
    )
    _ship_bundled(host)
    assert [r["id"] for r in installer.check_updates()] == ["cowork"]


def test_not_older_warning_stays_quiet_when_the_bundled_copy_is_newer(host, monkeypatch, caplog):
    """Control: in a correctly versioned move the extra warning never fires, so it can't
    become per-load noise."""
    from graph.config import LangGraphConfig

    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host, version="0.4.0")
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [host.bundled, host.live])
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))
    assert "not older than the bundled one" not in caplog.text


# ── unloading a superseded member THIS process was running (all three call sites) ───


def _loaded_from_the_installed_copy(monkeypatch, host, pid: str = "cowork") -> types.ModuleType:
    """Stand in for a process that imported the INSTALLED copy — what a server that was
    upgraded without a restart is still running."""
    mod = types.ModuleType(MODULE)
    mod.__path__ = [str(host.live / pid)]
    mod.__file__ = str(host.live / pid / "__init__.py")
    monkeypatch.setitem(sys.modules, MODULE, mod)
    return mod


async def test_uninstall_bundle_unloads_a_superseded_member_it_was_running(host, monkeypatch):
    from ops import OpContext
    from ops.plugins import uninstall_bundle

    bundle = _bundle_repo(host, enabled=["cowork", "google"])
    installer.install(bundle)
    _write_config(host, {"plugins": {"enabled": ["cowork", "google"]}})
    _ship_bundled(host)
    _loaded_from_the_installed_copy(monkeypatch, host)
    reloads: list = []

    rep = await uninstall_bundle(
        "cowork-archetype",
        ctx=OpContext(knowledge_store=None, graph_config=None),
        apply_settings=lambda updates: reloads.append(updates) or (True, []),
    )
    assert rep["superseded"] == ["cowork"] and rep["superseded_was_loaded"] == ["cowork"]
    assert MODULE not in sys.modules  # the copy it was running went — so it was unloaded
    assert reloads and "cowork" in _read_config(host)["plugins"]["enabled"]


async def test_update_bundle_unloads_a_superseded_member_it_was_running(host, monkeypatch):
    from ops import OpContext
    from ops.plugins import update_bundle

    bundle = _bundle_repo(host, enabled=["cowork", "google"])
    installer.install(bundle)
    _write_config(host, {"plugins": {"enabled": ["cowork", "google"]}})
    _ship_bundled(host)
    _loaded_from_the_installed_copy(monkeypatch, host)
    cfg = types.SimpleNamespace(plugins_enabled=["cowork", "google"], plugins_disabled=[])

    res = await update_bundle(
        "cowork-archetype",
        ctx=OpContext(knowledge_store=None, graph_config=cfg),
        apply_settings=lambda updates: (True, []),
    )
    assert res.removed_members == ["cowork"] and res.retire_error is None
    assert MODULE not in sys.modules


async def test_devkit_uninstall_unloads_a_superseded_copy_it_was_running(host, monkeypatch):
    mod, applied = _devkit(monkeypatch)
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    _loaded_from_the_installed_copy(monkeypatch, host)
    out = await mod.uninstall_plugin.ainvoke({"plugin_id": "cowork"})
    assert "removed the superseded copy" not in out and out.startswith("✓ uninstalled cowork")
    assert MODULE not in sys.modules and applied == [None]  # purged + reloaded


# ── every lifecycle op acts on the root the loader reads (`plugins.dir`) ───────────


def test_lifecycle_operations_follow_the_configured_plugins_dir(host):
    """Removal, the inventory and sync all have to use the dir the LOADER reads. Using
    the instance dir while the loader read the configured one let a superseded uninstall
    drop the lock row and leave the copy on disk — where, now untracked, it can shadow
    the bundled copy again (#1574)."""
    alt = host.home / "alt-plugins"
    _write_plugin(alt / "cowork", "cowork", "0.3.1")
    host.lock.write_text(json.dumps({"plugins": [{"id": "cowork", "source_url": UPSTREAM}]}))
    _ship_bundled(host)
    _write_config(host, {"plugins": {"dir": str(alt), "enabled": ["cowork"]}})

    assert installer.live_plugins_dir() == alt
    [row] = [r for r in installer.list_installed() if r["id"] == "cowork"]
    assert row["superseded"] is True and row["copy_on_disk"] is True  # the ALT copy was seen
    assert installer.sync() == [{"id": "cowork", "status": "present"}]

    assert installer.uninstall("cowork")["superseded_by_bundled"] == "0.4.0"
    assert not (alt / "cowork").exists()  # the copy that actually loads is what went
    assert installer._read_lock()["plugins"] == []
    assert _read_config(host)["plugins"]["enabled"] == ["cowork"]  # still on
    assert installer.list_installed() == []


def test_a_removal_that_cannot_rename_raises_install_error(host):
    """A failed removal must reach callers as InstallError (they all handle it) — a bare
    OSError is a 500 with a traceback instead of "couldn't remove it"."""
    _remote(host, "protoLabsAI", "cowork-plugin", "cowork", "0.3.1", tags=["v0.3.1"])
    _old_host_install(host)
    _ship_bundled(host)
    host.live.chmod(0o500)  # the copy's PARENT is read-only → rename can't move it aside
    try:
        with pytest.raises(installer.InstallError, match="could not remove the installed copy"):
            installer.uninstall("cowork")
    except Failed:  # pragma: no cover — a platform where the rename still succeeds
        pytest.skip("this platform allows the rename with a read-only parent")
    finally:
        host.live.chmod(0o700)
    # Nothing was half-done: the copy is still there, still recorded, still ignored.
    assert (host.live / "cowork").exists() and installer._read_lock()["plugins"]


def test_the_autoupdate_skip_log_redacts_the_install_url(host, monkeypatch, caplog):
    from server import agent_init

    secret_url = "https://x-access-token:SEKRET@git.example.test/protoLabsAI/cowork-plugin"
    _superseded_pair(host, recorded=secret_url)
    cfg = types.SimpleNamespace(plugins_enabled=["cowork"], plugins_disabled=[], plugins_sources_allow=[])
    monkeypatch.setattr(agent_init, "_apply_settings_changes", lambda **kw: (True, []))
    with caplog.at_level(logging.INFO):
        updated = asyncio.run(
            agent_init._plugin_autoupdate_sweep(cfg, {"cowork": {"track": "main", "when": "always"}})
        )
    assert updated == 0
    assert "ships with protoAgent now" in caplog.text and "SEKRET" not in caplog.text
