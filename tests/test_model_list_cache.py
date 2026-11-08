"""Provider model lists are cached, single-flighted, and never dropped on a slow probe.

Stress test of v0.231.0 (health track): under 8 concurrent ``GET /api/settings/schema``
calls, 2 of 8 responses lost all ten ``openai-codex:*`` slot options, and every call
made four live provider ``/models`` probes (1.2-1.9 s each page load). Nothing cached
the lists, and a Codex probe that failed under the load came back as an empty lane.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from graph.config import LangGraphConfig
from graph.providers import discovery, model_cache

CODEX = [f"gpt-6.1-sol-{i}" for i in range(10)]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(model_cache, "_clock", c)
    return c


@pytest.fixture
def codex_lane(monkeypatch):
    """Only the ChatGPT subscription is signed in; the gateway has no key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, p == "openai-codex", "instance_store", "", "")
    )
    calls = {"n": 0}
    answers: list = []  # each probe pops the next answer; the last one repeats

    def _probe(provider, cfg):
        assert provider in ("openai-codex", "anthropic-oauth")
        calls["n"] += 1
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(discovery, "list_provider_models", _probe)
    return types.SimpleNamespace(calls=calls, answers=answers)


def _cfg() -> LangGraphConfig:
    return LangGraphConfig(api_base="https://gw.example.com/v1", api_key="")


def _codex_options(opts: list[str]) -> list[str]:
    return [o for o in opts if o.startswith("openai-codex:")]


def test_concurrent_callers_share_one_probe_and_all_see_the_lane(codex_lane, clock, monkeypatch):
    """The stress-test shape: eight Settings loads at once. One probe, eight full answers."""
    gate = threading.Event()

    def _slow_probe(provider, cfg):
        codex_lane.calls["n"] += 1
        gate.wait(5)
        return CODEX, ""

    monkeypatch.setattr(discovery, "list_provider_models", _slow_probe)
    results: list[list[str]] = []
    lock = threading.Lock()

    def _load():
        opts = discovery.qualified_model_options(_cfg())
        with lock:
            results.append(opts)

    threads = [threading.Thread(target=_load) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.2)  # let every thread reach the probe or the single-flight lock
    gate.set()
    for t in threads:
        t.join(10)

    assert codex_lane.calls["n"] == 1
    assert len(results) == 8
    assert all(_codex_options(r) == [f"openai-codex:{m}" for m in CODEX] for r in results)


def test_a_fresh_list_is_served_without_a_probe_and_the_ttl_expires(codex_lane, clock):
    codex_lane.answers[:] = [(CODEX, ""), (CODEX + ["gpt-new"], "")]

    first = discovery.qualified_model_options(_cfg())
    clock.now += model_cache.TTL_S - 1
    second = discovery.qualified_model_options(_cfg())

    assert codex_lane.calls["n"] == 1  # opening Settings again inside the TTL costs nothing
    assert first == second

    clock.now += 2  # past the TTL
    third = discovery.qualified_model_options(_cfg())

    assert codex_lane.calls["n"] == 2
    assert "openai-codex:gpt-new" in third


def test_a_failed_probe_keeps_the_last_good_list_marked_stale(codex_lane, clock):
    """The bug itself: a transient failure must not make ten options vanish."""
    codex_lane.answers[:] = [(CODEX, ""), ([], "Could not list Codex models: ReadTimeout")]

    discovery.qualified_model_options(_cfg())
    clock.now += model_cache.TTL_S + 1
    lanes = {lane["provider"]: lane for lane in discovery.available_model_lanes(_cfg())}

    assert codex_lane.calls["n"] == 2  # it did try again
    codex = lanes["openai-codex"]
    assert codex["models"] == CODEX
    assert codex["stale"] is True
    assert "ReadTimeout" in codex["error"]
    # ...and the slot options still offer every model.
    assert len(_codex_options(discovery.qualified_model_options(_cfg()))) == 10


def test_a_probe_that_raises_also_keeps_the_last_good_list(codex_lane, clock):
    codex_lane.answers[:] = [(CODEX, ""), RuntimeError("connection reset")]

    discovery.qualified_model_options(_cfg())
    clock.now += model_cache.TTL_S + 1

    assert len(_codex_options(discovery.qualified_model_options(_cfg()))) == 10


def test_after_a_failure_the_retry_waits_then_recovers(codex_lane, clock):
    codex_lane.answers[:] = [(CODEX, ""), ([], "timeout"), (CODEX[:3], "")]

    discovery.qualified_model_options(_cfg())
    clock.now += model_cache.TTL_S + 1
    discovery.qualified_model_options(_cfg())  # fails, serves stale
    clock.now += model_cache.RETRY_S - 1
    discovery.qualified_model_options(_cfg())
    assert codex_lane.calls["n"] == 2  # no hammering a provider that just failed

    clock.now += 2
    lanes = {lane["provider"]: lane for lane in discovery.available_model_lanes(_cfg())}
    assert codex_lane.calls["n"] == 3
    assert lanes["openai-codex"]["models"] == CODEX[:3]
    assert "stale" not in lanes["openai-codex"]
    assert lanes["openai-codex"]["error"] == ""


def test_a_failure_with_nothing_to_fall_back_on_is_reported_and_retried_soon(codex_lane, clock):
    codex_lane.answers[:] = [([], "timeout"), (CODEX, "")]

    lanes = {lane["provider"]: lane for lane in discovery.available_model_lanes(_cfg())}
    assert lanes["openai-codex"]["models"] == [] and lanes["openai-codex"]["error"] == "timeout"

    clock.now += model_cache.NEGATIVE_TTL_S + 1
    assert len(_codex_options(discovery.qualified_model_options(_cfg()))) == 10


def test_an_explicit_refresh_always_probes_and_reports_its_own_failure(codex_lane, clock):
    codex_lane.answers[:] = [(CODEX, ""), ([], "timeout"), (CODEX[:2], "")]

    discovery.cached_provider_models("openai-codex", _cfg())
    got = discovery.cached_provider_models("openai-codex", _cfg(), refresh=True)
    assert codex_lane.calls["n"] == 2
    assert got.models == [] and got.error == "timeout"  # honest answer to "Get models"

    # the cached list survives a failed refresh...
    assert discovery.cached_provider_models("openai-codex", _cfg()).models == CODEX
    # ...and a successful refresh replaces it.
    discovery.cached_provider_models("openai-codex", _cfg(), refresh=True)
    assert discovery.cached_provider_models("openai-codex", _cfg()).models == CODEX[:2]


def test_signing_in_or_out_forgets_that_providers_list(codex_lane, clock):
    from graph.providers import oauth

    codex_lane.answers[:] = [(CODEX, ""), (CODEX[:1], "")]
    discovery.qualified_model_options(_cfg())

    oauth.clear_disconnected("openai-codex")  # every in-console sign-in path calls this

    assert _codex_options(discovery.qualified_model_options(_cfg())) == [f"openai-codex:{CODEX[0]}"]
    assert codex_lane.calls["n"] == 2


def test_gateway_lists_are_keyed_by_endpoint_and_never_store_the_key(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(
        "graph.config_io.list_gateway_models", lambda base, key, **kw: (calls.append(base) or [base[-2:]], "")
    )

    assert discovery.cached_gateway_models("https://a/v1", "secret-key").models == ["v1"]
    discovery.cached_gateway_models("https://a/v1", "secret-key")
    discovery.cached_gateway_models("https://b/v2", "secret-key")

    assert calls == ["https://a/v1", "https://b/v2"]
    assert all("secret-key" not in repr(k) for k in model_cache._entries)


def test_settings_schema_does_not_reprobe_on_every_load(monkeypatch, tmp_path, clock):
    """Route level: two Settings loads, one probe per lane."""
    import runtime.state as rs
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.config_routes import register_config_routes

    monkeypatch.setattr(
        rs.STATE,
        "graph_config",
        types.SimpleNamespace(model_provider="openai-codex", api_base="", api_key=""),
        raising=False,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(discovery, "list_provider_models", lambda p, cfg: (calls.append(p) or CODEX, ""))
    monkeypatch.setattr(
        discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, p == "openai-codex", "instance_store", "", "")
    )
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: tmp_path / "absent.yaml")
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    captured = []
    monkeypatch.setattr(
        "graph.settings_schema.build_schema",
        lambda cfg, model_options=None, slot_model_options=None, **kw: captured.append(slot_model_options) or [],
    )
    app = FastAPI()
    register_config_routes(app)
    client = TestClient(app)

    client.get("/api/settings/schema")
    client.get("/api/settings/schema")

    assert calls == ["openai-codex"]  # was 2 probes per load
    assert captured[0] == captured[1] and len(_codex_options(captured[1])) == 10
