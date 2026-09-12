"""The ``plugin_setup`` setup-gap action and the setup steps behind it: a banner button
that runs a command the reporting plugin registered (``registry.register_setup_step``), via
the core route ``POST /api/plugin-setup/<id>/<step>``. The action is data naming a step; the
host runs only the callable it holds for exactly that (plugin, step). Also the generation
fence that keeps a cleared plugin's late threads from re-raising its banners."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.plugins import setup_gaps
from graph.plugins.registry import PluginRegistry
from graph.plugins.testkit import FakeRegistry


@pytest.fixture(autouse=True)
def _clean():
    setup_gaps.reset()
    yield
    setup_gaps.reset()


def _actions(action) -> list[dict] | None:
    setup_gaps.report("pb", "cli", "the CLI is missing", label="Project Board", action=action)
    [gap] = setup_gaps.active()
    return gap.get("actions")


# ── the action: sanitized like every other kind ──────────────────────────────────


def test_plugin_setup_is_an_allowlisted_kind():
    assert "plugin_setup" in setup_gaps.ACTION_KINDS


def test_a_plugin_setup_action_keeps_its_step_and_is_scoped_to_the_reporting_plugin():
    actions = _actions({"kind": "plugin_setup", "step": "download-cli", "label": "Download the CLI",
                        "target": "some_other_plugin", "command": "rm -rf /", "url": "https://evil.example"})
    assert actions == [{"kind": "plugin_setup", "target": "pb", "step": "download-cli", "label": "Download the CLI"}]
    json.dumps(actions)  # plain data


@pytest.mark.parametrize("step", [None, "", "   ", "Download", "../etc", "a/b", "a.b", "http://x", "x:y", "a" * 65, 5,
                                  ["download-cli"]])
def test_a_plugin_setup_action_without_a_valid_step_is_dropped(step):
    assert _actions({"kind": "plugin_setup", "step": step, "label": "Go"}) is None


def test_a_plugin_setup_action_sits_beside_a_config_action():
    actions = _actions([{"kind": "plugin_setup", "step": "install-chrome", "label": "Install Chrome"},
                        {"kind": "plugin_config", "label": "Configure", "fields": ["binary"]}])
    assert [a["kind"] for a in actions] == ["plugin_setup", "plugin_config"]


# ── the steps: held server-side, run and normalized ──────────────────────────────


def test_register_run_and_normalize():
    assert setup_gaps.register_step("pb", "go", lambda: "  all\n set  ") is True
    assert setup_gaps.run_step("pb", "go") == {"ok": True, "message": "all set", "pending": False}
    setup_gaps.register_step("pb", "go", lambda: {"ok": True, "pending": True, "message": "started"})  # replaces
    assert setup_gaps.run_step("pb", "go") == {"ok": True, "message": "started", "pending": True}
    setup_gaps.register_step("pb", "nope", lambda: {"ok": False, "message": "no", "pending": True})
    assert setup_gaps.run_step("pb", "nope") == {"ok": False, "message": "no", "pending": False}
    setup_gaps.register_step("pb", "quiet", lambda: None)
    assert setup_gaps.run_step("pb", "quiet") == {"ok": True, "message": "", "pending": False}
    assert setup_gaps.run_step("pb", "missing") is None
    assert setup_gaps.run_step("other", "go") is None


def test_a_step_that_raises_is_not_ok_with_its_message():
    def boom():
        raise RuntimeError("the network is down")

    setup_gaps.register_step("pb", "go", boom)
    assert setup_gaps.run_step("pb", "go") == {"ok": False, "message": "RuntimeError: the network is down",
                                               "pending": False}


def test_a_long_message_is_bounded():
    setup_gaps.register_step("pb", "go", lambda: "x" * 5000)
    assert len(setup_gaps.run_step("pb", "go")["message"]) == setup_gaps.MAX_MESSAGE_CHARS


@pytest.mark.parametrize("step,fn", [("Bad", lambda: 1), ("a/b", lambda: 1), ("", lambda: 1), ("ok", "not callable")])
def test_bad_registrations_are_refused(step, fn):
    assert setup_gaps.register_step("pb", step, fn) is False
    assert not setup_gaps.has_step("pb", step)


def test_steps_per_plugin_are_capped():
    for i in range(setup_gaps.MAX_STEPS_PER_PLUGIN):
        assert setup_gaps.register_step("pb", f"s{i}", lambda: 1)
    assert setup_gaps.register_step("pb", "one-too-many", lambda: 1) is False
    assert setup_gaps.register_step("pb", "s0", lambda: 2) is True  # replacing isn't growth


def test_disable_and_uninstall_drop_the_steps_with_the_gaps():
    setup_gaps.register_step("pb", "go", lambda: 1)
    setup_gaps.register_step("gh", "login", lambda: 1)
    setup_gaps.clear_plugin("pb")                        # disabled
    assert not setup_gaps.has_step("pb", "go") and setup_gaps.has_step("gh", "login")
    setup_gaps.retain({"other"})                         # gh uninstalled
    assert not setup_gaps.has_step("gh", "login")


def test_the_registry_method_forwards_under_the_plugins_own_id(tmp_path, caplog):
    reg = PluginRegistry("pb", tmp_path)
    reg.register_setup_step("download-cli", lambda: "ok")
    assert setup_gaps.run_step("pb", "download-cli")["message"] == "ok"
    reg.register_setup_step("Not An Id", lambda: "ok")
    assert "refused" in caplog.text and not setup_gaps.has_step("pb", "Not An Id")


def test_the_testkit_fake_records_steps_with_the_host_signature():
    fake = FakeRegistry({}, plugin_id="pb")
    fake.register_setup_step("download-cli", lambda: {"ok": True, "pending": True})
    assert fake.setup_steps["download-cli"]()["pending"] is True


# ── the generation fence: a cleared plugin's late threads raise no ghost banner ──


def test_a_registry_from_before_a_clear_reports_nothing(tmp_path):
    old = PluginRegistry("pb", tmp_path)
    old.report_setup_gap("cli", "the CLI is missing")
    setup_gaps.clear_plugin("pb")                          # the operator disabled it
    # …and a download thread of that load finishes afterwards, re-reporting with a Retry
    old.report_setup_gap("cli", "the download failed", action={"kind": "plugin_setup", "step": "download-cli"})
    assert setup_gaps.active() == []
    fresh = PluginRegistry("pb", tmp_path)                # re-enabled: the new load reports normally
    fresh.report_setup_gap("cli", "the CLI is missing")
    assert [g["key"] for g in setup_gaps.active()] == ["cli"]
    old.report_setup_gap("late", "still the old load")    # …and the old one still can't
    assert [g["key"] for g in setup_gaps.active()] == ["cli"]


def test_an_uninstall_retires_the_generation_too(tmp_path):
    old = PluginRegistry("pb", tmp_path)
    setup_gaps.retain({"other"})                           # pb is gone from disk
    old.report_setup_gap("cli", "late")
    assert setup_gaps.active() == []


def test_host_side_reports_are_not_fenced():
    """The host reports some gaps under a plugin's id itself (a superseded copy on disk, for a
    plugin that is merely OFF) — those carry no registry generation and must still land."""
    setup_gaps.clear_plugin("pb")
    setup_gaps.report("pb", "superseded", "an ignored copy is on disk")
    assert [g["key"] for g in setup_gaps.active()] == ["superseded"]


def test_a_plugin_whose_reload_fails_loses_its_steps_and_banners(tmp_path, monkeypatch):
    from graph.config import LangGraphConfig
    from graph.plugins import loader as plugin_loader

    root = tmp_path / "plugins"
    plugin = root / "boomy"
    plugin.mkdir(parents=True)
    (plugin / "protoagent.plugin.yaml").write_text("id: boomy\nname: Boomy\nenabled: true\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register(registry):\n"
        "    registry.register_setup_step('go', lambda: 'ran the OLD code')\n"
        "    registry.report_setup_gap('cli', 'missing', action={'kind': 'plugin_setup', 'step': 'go'})\n",
        encoding="utf-8")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda _config: [root])
    first = plugin_loader.load_plugins(LangGraphConfig(plugins_enabled=["boomy"]))
    assert first.meta[0].get("loaded") is True
    assert setup_gaps.has_step("boomy", "go") and [g["key"] for g in setup_gaps.active()] == ["cli"]

    (plugin / "__init__.py").write_text("def register(registry):\n    raise RuntimeError('broken edit')\n",
                                        encoding="utf-8")
    plugin_loader.purge_plugin_modules("boomy")            # what a reload does before re-importing
    second = plugin_loader.load_plugins(LangGraphConfig(plugins_enabled=["boomy"]))
    assert "broken edit" in str(second.meta[0].get("error"))
    # no live code behind them any more: the old load's step is unreachable, its banner gone
    assert not setup_gaps.has_step("boomy", "go") and setup_gaps.active() == []


# ── the route the banner button calls ────────────────────────────────────────────

ROUTE = "/api/plugin-setup/{plugin}/{step}"


def _client(monkeypatch, audits=None):
    from graph.plugins import installer
    from operator_api.plugin_routes import register_plugin_routes

    monkeypatch.setattr(installer, "_audit", lambda *a, **k: audits.append((a, k)) if audits is not None else None)
    app = FastAPI()
    register_plugin_routes(app)
    return TestClient(app)


def test_the_route_runs_the_registered_step_and_audits_it(monkeypatch):
    audits: list = []
    setup_gaps.register_step("pb", "download-cli", lambda: {"ok": True, "pending": True, "message": "Downloading…"})
    r = _client(monkeypatch, audits).post(ROUTE.format(plugin="pb", step="download-cli"))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Downloading…", "pending": True}
    [(args, kwargs)] = audits
    assert args[0] == "setup_step" and args[1] == {"id": "pb", "step": "download-cli"} and kwargs["success"] is True


def test_the_route_404s_a_step_the_plugin_never_registered_and_never_reaches_anothers(monkeypatch):
    ran = []
    setup_gaps.register_step("pb", "download-cli", lambda: ran.append(1) or "ok")
    client = _client(monkeypatch)
    assert client.post(ROUTE.format(plugin="other", step="download-cli")).status_code == 404
    assert client.post(ROUTE.format(plugin="pb", step="install-chrome")).status_code == 404
    # …and nothing answers inside the plugin-exemptable namespace any more
    assert client.post("/api/plugins/pb/setup-steps/download-cli").status_code in (404, 405)
    assert ran == []


def test_the_route_reports_a_raising_step_as_not_ok_never_a_500(monkeypatch):
    def boom():
        raise OSError("disk full")

    setup_gaps.register_step("pb", "go", boom)
    r = _client(monkeypatch).post(ROUTE.format(plugin="pb", step="go"))
    assert r.status_code == 200 and r.json() == {"ok": False, "message": "OSError: disk full", "pending": False}


def test_a_disabled_plugins_button_404s(monkeypatch):
    setup_gaps.register_step("pb", "go", lambda: "ok")
    setup_gaps.clear_plugin("pb")
    assert _client(monkeypatch).post(ROUTE.format(plugin="pb", step="go")).status_code == 404


def test_the_setup_route_takes_the_operator_credential_whatever_a_manifest_exempts():
    """A plugin may exempt its OWN namespace from the auth gate (``public_paths``) or open it to
    fleet peers (``federation_paths``) — /plugins/<id>/ and /api/plugins/<id>/. The setup route
    is core and lives OUTSIDE both, so even a manifest claiming its whole namespace through
    both keys can't let an anonymous caller — or a fleet peer — run its steps."""
    from a2a_impl import auth
    from graph.plugins.manifest import _parse_public_paths

    route = ROUTE.format(plugin="agent_browser", step="install-chrome")
    claimed = ["/api/plugins/agent_browser/", "/plugins/agent_browser/"]
    saved = (list(auth._PLUGIN_PUBLIC), list(auth._PLUGIN_FEDERATION))
    try:
        auth.set_public_prefixes(_parse_public_paths(claimed, "agent_browser"))
        auth.set_federation_prefixes(_parse_public_paths(claimed, "agent_browser", kind="federation_path"))
        # the exemptions really are in force on the plugin's own subtree…
        assert auth._is_public("/api/plugins/agent_browser/anything")
        assert not auth._requires_operator("/api/plugins/agent_browser/anything")
        # …and do not reach the core setup route
        assert auth._is_public(route) is False
        assert auth._requires_operator(route) is True
    finally:
        auth.set_public_prefixes(saved[0])
        auth.set_federation_prefixes(saved[1])
