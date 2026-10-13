"""Core plugin-lifecycle routes stay gated whatever a plugin manifest declares (unit level).

The real-process proof is tests/test_core_plugin_routes_never_exempt_real_process.py; this
file pins the pieces and — the part that keeps it true — checks the LIVE core route table:
every core ``/api/plugins/…`` route a plugin namespace prefix could reach must be carved out.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI

from a2a_impl import auth
from graph.plugins import manifest as mf


@pytest.fixture(autouse=True)
def _reset_prefixes():
    yield
    auth.set_public_prefixes([])
    auth.set_federation_prefixes([])


def _core_plugin_paths() -> list[str]:
    from operator_api.plugin_routes import register_plugin_routes

    app = FastAPI()
    register_plugin_routes(app)
    paths = {getattr(r, "path", "") for r in app.routes}
    return sorted(p for p in paths if p.startswith("/api/plugins"))


def test_every_core_route_a_plugin_namespace_could_reach_is_carved_out():
    # Fill each path parameter with a plausible plugin / bundle id: if the concrete path
    # sits inside a plugin's /api/plugins/<id>/ subtree, a manifest prefix of that subtree
    # would cover it — so it MUST be a recognised core route. A new core route added under
    # /api/plugins/{plugin_id}/… fails here until the carve-out learns it.
    paths = _core_plugin_paths()
    assert "/api/plugins/{plugin_id}/update" in paths and "/api/plugins/{plugin_id}/enabled" in paths
    for template in paths:
        concrete = re.sub(r"\{[^}]+\}", "someplugin", template)
        if auth._PLUGIN_NS_RE.match(concrete):
            assert auth.is_core_plugin_route(concrete), template
            assert auth.is_core_plugin_route(concrete + "/") or concrete.endswith("/"), template


def test_manifest_and_middleware_share_one_pattern():
    assert mf._CORE_PLUGIN_ROUTE_RE.pattern == auth._CORE_PLUGIN_ROUTE_RE.pattern


def test_a_plugin_public_prefix_never_exempts_core_lifecycle_routes():
    auth.set_public_prefixes(["/api/plugins/evil/"])
    assert auth._is_public("/api/plugins/evil/hook")  # its own route: still public
    for path in ("/api/plugins/evil/update", "/api/plugins/evil/enabled", "/api/plugins/evil/update/"):
        assert not auth._is_public(path), path

    auth.set_public_prefixes(["/api/plugins/bundles/"])
    assert not auth._is_public("/api/plugins/bundles/x/update")
    assert not auth._is_public("/api/plugins/bundles/x")


def test_a_plugin_federation_prefix_never_lowers_the_ceiling_on_core_routes():
    auth.set_federation_prefixes(["/api/plugins/evil/"])
    assert not auth._requires_operator("/api/plugins/evil/sync-store")  # its own route
    assert auth._requires_operator("/api/plugins/evil/update")
    assert auth._requires_operator("/api/plugins/evil/enabled")


def test_the_manifest_drops_a_path_that_names_a_core_route_and_reserves_bundles(tmp_path):
    kept = mf._parse_public_paths(
        ["/api/plugins/evil/update", "/api/plugins/evil/enabled/", "/api/plugins/evil/", "/plugins/evil/view"],
        "evil",
    )
    assert kept == ["/api/plugins/evil/", "/plugins/evil/view"]

    for pid in ("bundles", "ack", "install-deps"):
        d = tmp_path / pid
        d.mkdir()
        (d / "protoagent.plugin.yaml").write_text(f"id: {pid}\nname: x\n", encoding="utf-8")
        assert mf.load_manifest(d) is None, pid
