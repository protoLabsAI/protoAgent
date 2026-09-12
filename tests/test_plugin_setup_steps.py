"""The ``plugin_setup`` setup-gap action and the setup steps behind it: a banner button
that runs a command the reporting plugin registered (``registry.register_setup_step``), via
``POST /api/plugins/<id>/setup-steps/<step>``. The action is data naming a step; the host
runs only the callable it holds for exactly that (plugin, step)."""

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


# ── the route the banner button calls ────────────────────────────────────────────


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
    r = _client(monkeypatch, audits).post("/api/plugins/pb/setup-steps/download-cli")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Downloading…", "pending": True}
    [(args, kwargs)] = audits
    assert args[0] == "setup_step" and args[1] == {"id": "pb", "step": "download-cli"} and kwargs["success"] is True


def test_the_route_404s_a_step_the_plugin_never_registered_and_never_reaches_anothers(monkeypatch):
    ran = []
    setup_gaps.register_step("pb", "download-cli", lambda: ran.append(1) or "ok")
    client = _client(monkeypatch)
    assert client.post("/api/plugins/other/setup-steps/download-cli").status_code == 404
    assert client.post("/api/plugins/pb/setup-steps/install-chrome").status_code == 404
    assert ran == []


def test_the_route_reports_a_raising_step_as_not_ok_never_a_500(monkeypatch):
    def boom():
        raise OSError("disk full")

    setup_gaps.register_step("pb", "go", boom)
    r = _client(monkeypatch).post("/api/plugins/pb/setup-steps/go")
    assert r.status_code == 200 and r.json() == {"ok": False, "message": "OSError: disk full", "pending": False}


def test_a_disabled_plugins_button_404s(monkeypatch):
    setup_gaps.register_step("pb", "go", lambda: "ok")
    setup_gaps.clear_plugin("pb")
    assert _client(monkeypatch).post("/api/plugins/pb/setup-steps/go").status_code == 404
