"""Device credential scope — the credential-plane ceiling (ADR 0121 D4, #4336 S7.2).

A paired phone is OPERATOR trust tier with ``device`` credential scope: it can use the whole
product but cannot change who can get in. These tests pin:

* the ceiling table × {bare, ``/agents/<slug>/`` proxied} × {device → 403, full → allowed},
  and that the table covers the real lifecycle/pairing/secrets routes in ``operator_api``;
* self rename/unpair allowed, another device's refused; promotion needs a full caller;
* credential-plane config keys refused for device scope at the config write path;
* legacy rows read ``full``; new phone pairings ``device``; agent pairings ``full``;
* SSE tokens carry the scope (and a scope-less one reads ``device``);
* the fleet proxy refuses BEFORE the ADR 0089 D3 swap (HTTP and the WS query token);
* ``trust_tier`` stays ``operator`` for a device, so chat/goals/knowledge are unaffected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import ast
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request  # module level: FastAPI resolves the stringized annotation here

# Patches infra.tailscale (so nothing can run a real `tailscale serve`) — platform-branching infra.
pytestmark = pytest.mark.platform_sensitive

_SECRET = "op-secret"


@pytest.fixture
def devices(tmp_path, monkeypatch):
    """A `security.devices` bound to a throwaway instance root."""
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-device-scope")

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


@pytest.fixture
def auth(devices):
    from a2a_impl import auth as mod

    mod.configure(bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    yield mod


def _phone(devices, name="phone"):
    code, _ = devices.start_pairing()
    device, token = devices.claim_pairing(code, name)
    return device, token


def _hub(devices, name="hub"):
    code, _ = devices.start_pairing(devices.KIND_AGENT)
    device, token = devices.claim_pairing(code, name)
    return device, token


def _never(*_a, **_k):
    raise AssertionError("a test must never reach tailscale or probe the network")


def _app(auth, monkeypatch, *, pairing: bool):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import infra.tailscale
    import operator_api.pairing_routes as pr

    # Belt and braces: nothing here may run a real `tailscale serve` (or any probe).
    monkeypatch.setattr(infra.tailscale, "enable_serve", _never)
    monkeypatch.setattr(infra.tailscale, "disable_serve", _never)
    monkeypatch.setattr(pr, "_offer_hosts", _never)
    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    app = FastAPI()
    if pairing:
        pr.register_pairing_routes(app, agent_name=lambda: "box")

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def _echo(path: str, request: Request):
        return {
            "path": path,
            "trust_tier": getattr(request.state, "trust_tier", None),
            "scope": getattr(request.state, "credential_scope", None),
            "device_id": getattr(request.state, "device_id", None),
        }

    app.add_middleware(auth.A2AAuthMiddleware)
    return TestClient(app)


@pytest.fixture
def client(auth, devices, monkeypatch):
    """Middleware + the real pairing routes, then an echo catch-all that reports what the
    middleware stamped (so a 200 from it means "the ceiling let it through")."""
    return _app(auth, monkeypatch, pairing=True)


@pytest.fixture
def echo(auth, devices, monkeypatch):
    """Middleware + ONLY the echo route: every path that passes the ceiling answers 200."""
    return _app(auth, monkeypatch, pairing=False)


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


# ── The ceiling table ─────────────────────────────────────────────────────────────────────


def _concrete(rule_path: str) -> str:
    return rule_path.replace("{id}", "someid").replace("/**", "/x")


def _cases():
    from a2a_impl.credential_scope import CEILING

    for rule in CEILING:
        for method in sorted(rule.methods - {"HEAD"}):
            for prefix in ("", "/agents/peer"):
                yield pytest.param(method, prefix + _concrete(rule.path), id=f"{method} {prefix}{rule.path}")


@pytest.mark.parametrize("method, path", list(_cases()))
def test_device_scope_is_refused_every_ceiling_path(client, devices, method, path):
    _, token = _phone(devices)
    resp = client.request(method, path, headers=_hdr(token), json={})
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error"] == "device_scope"
    assert body["detail"]["code"] == "device_scope"
    assert "full-access device" in body["detail"]["reason"]


@pytest.mark.parametrize("method, path", list(_cases()))
def test_full_scope_is_never_refused_by_the_ceiling(echo, method, path):
    resp = echo.request(method, path, headers=_hdr(_SECRET), json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "full"


@pytest.mark.parametrize("method, path", list(_cases()))
def test_an_agent_device_is_full_scope(echo, devices, method, path):
    _, token = _hub(devices)
    resp = echo.request(method, path, headers=_hdr(token), json={})
    assert resp.status_code == 200, resp.text


def test_a_trailing_slash_does_not_slip_past(client, devices):
    _, token = _phone(devices)
    assert client.post("/api/pairing/start/", headers=_hdr(token)).status_code == 403
    assert client.post("/agents/peer/api/plugins/install/", headers=_hdr(token)).status_code == 403


def test_reads_and_the_rest_of_the_product_stay_open_to_a_device(echo, devices):
    _, token = _phone(devices)
    for method, path in [
        ("GET", "/api/devices"),
        ("GET", "/api/pairing/reach"),
        ("GET", "/api/plugins/installed"),
        ("GET", "/api/config/oauth-status"),
        ("POST", "/api/chat"),
        ("POST", "/api/goals"),
        ("POST", "/api/knowledge/ingest"),
        ("POST", "/api/plugins/notes/items"),  # a plugin's own data route
        ("GET", "/agents/peer/api/plugins/installed"),
    ]:
        resp = echo.request(method, path, headers=_hdr(token), json={})
        assert resp.status_code == 200, (method, path, resp.text)


_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _literal_methods(node) -> set[str]:
    """``methods=[...]`` of an ``api_route``/``add_api_route`` call (FastAPI's default: GET)."""
    kw = next((k.value for k in node.keywords if k.arg == "methods"), None)
    if isinstance(kw, (ast.List, ast.Tuple, ast.Set)):
        return {e.value.upper() for e in kw.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return {"GET"}


def _routes_in_source(text: str) -> set[tuple[str, str]]:
    """Every ``(METHOD, "/api/...")`` route registered in ``text``, by any of FastAPI's forms:
    ``@<x>.<verb>(path)`` and ``@<x>.api_route(path, methods=…)`` decorators (``app``, ``router``,
    ``fastapi_app`` — any receiver), and ``<x>.add_api_route(path, endpoint, methods=…)`` calls.

    Parsed, not grepped, so formatting can't hide a route; and a ``<verb>(…)`` CALL that isn't
    a decorator (``client.get("/api/…")`` in a proxy) is not mistaken for one."""
    out: set[tuple[str, str]] = set()

    def path_of(call):
        arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "path"), None)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("/api/"):
            return arg.value
        return None

    tree = ast.parse(text)
    for node in ast.walk(tree):
        calls = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [d for d in node.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)]
            calls = [(c, c.func.attr) for c in calls if c.func.attr in _HTTP_VERBS or c.func.attr == "api_route"]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_api_route":
            calls = [(node, "add_api_route")]
        for call, attr in calls:
            path = path_of(call)
            if path is None:
                continue
            methods = {attr.upper()} if attr in _HTTP_VERBS else _literal_methods(call)
            out.update((m, path) for m in methods)
    return out


def _operator_routes() -> set[tuple[str, str]]:
    """The operator ``/api`` routes of ``operator_api`` and ``server``, from their source.

    The source, not ``app.routes``: the server's app is assembled inside ``server._main`` (a
    full boot — graph, plugins, scheduler), which no unit test can afford. The parse is pinned
    against a real route table by `test_route_discovery_matches_a_real_route_table`."""
    root = Path(__file__).resolve().parents[1] / "src"
    out: set[tuple[str, str]] = set()
    for sub in ("operator_api", "server"):
        for f in sorted((root / sub).rglob("*.py")):
            out |= _routes_in_source(f.read_text("utf-8"))
    return out


def test_route_discovery_catches_every_registration_form():
    src = """
@app.get("/api/a")
async def a(): ...

@router.post(
    "/api/b",
    include_in_schema=False,
)
async def b(): ...

@router.api_route("/api/c", methods=["PUT", "DELETE"])
async def c(): ...

@router.api_route("/api/d")
async def d(): ...

router.add_api_route("/api/e", handler, methods=("PATCH",))
app.add_api_route(path="/api/f", endpoint=handler, methods={"POST"})

async def proxy(client):
    await client.get("/api/not-a-route")   # a call, not a registration

@app.get("/health")
async def health(): ...
"""
    assert _routes_in_source(src) == {
        ("GET", "/api/a"),
        ("POST", "/api/b"),
        ("PUT", "/api/c"),
        ("DELETE", "/api/c"),
        ("GET", "/api/d"),
        ("PATCH", "/api/e"),
        ("POST", "/api/f"),
    }


def test_route_discovery_matches_a_real_route_table():
    """The source parse agrees with FastAPI's own table for a module whose routes can be
    mounted cheaply — so a registration form the parse doesn't know shows up here."""
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    import operator_api.pairing_routes as pr

    def api_routes(routes, prefix=""):
        # FastAPI >= 0.141 includes a router lazily, as one wrapper holding the original.
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx = getattr(r, "include_context", None)
                yield from api_routes(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            elif isinstance(r, APIRoute):
                yield prefix + r.path, r.methods

    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    real = {(m, path) for path, methods in api_routes(app.routes) if path.startswith("/api/") for m in methods}
    parsed = _routes_in_source(Path(pr.__file__).read_text("utf-8"))
    assert real and parsed == real
    assert parsed <= _operator_routes()


def _route_example(route: str) -> str:
    return re.sub(r"\{[^}]+\}", "someid", route)


def test_every_ceiling_rule_names_a_real_route():
    """A renamed route would leave a dead rule — and a live route nobody refuses."""
    from a2a_impl.credential_scope import CEILING, _compile

    routes = _operator_routes()
    for rule in CEILING:
        pat = _compile(rule.path)
        hits = [(m, r) for (m, r) in routes if m in rule.methods and pat.search(_route_example(r))]
        assert hits, f"no operator route matches ceiling rule {sorted(rule.methods)} {rule.path}"


# Families where EVERY write route is on the credential plane. A new route here must either
# land in CEILING or be added to the explicit allow list below with a reason.
_PLANE_FAMILIES = (
    "/api/pairing/",
    "/api/plugin-setup/",
    "/api/secrets/",
    "/api/config/oauth/",
    "/api/plugins/",
    "/api/mcp/servers",
    "/api/devices/",
)
_ALLOWED_WRITES = {
    ("POST", "/api/pairing/claim"),  # unauthenticated by necessity (ADR 0087 D4) — no scope at all
    # A device rotating its OWN token (ADR 0121 D5) — it changes nothing about who can get in,
    # and a ceiling here would stop every phone from ever rotating.
    ("POST", "/api/devices/self/rotate"),
    # A device moving ITSELF to another of this agent's origins (ADR 0121 D1, #4336 S7.5): the
    # code it mints rebinds the same row (same id, name and scope) — it adds nobody — and the
    # route refuses every origin this instance doesn't offer.
    ("POST", "/api/devices/self/handoff"),
}


def test_every_write_route_in_a_plane_family_is_on_the_ceiling():
    from a2a_impl.credential_scope import ceiling_rule

    missing = []
    for method, route in sorted(_operator_routes()):
        if method == "GET" or not route.startswith(_PLANE_FAMILIES) or (method, route) in _ALLOWED_WRITES:
            continue
        if ceiling_rule(method, _route_example(route)) is None:
            missing.append(f"{method} {route}")
    assert not missing, f"write routes missing from the credential-plane ceiling: {missing}"


def test_s4_reachability_and_restart_routes_are_on_the_ceiling():
    from a2a_impl.credential_scope import ceiling_rule

    assert ceiling_rule("POST", "/api/pairing/tailscale/serve") is not None
    assert ceiling_rule("DELETE", "/api/pairing/tailscale/serve") is not None
    assert ceiling_rule("POST", "/api/restart") is not None
    assert ceiling_rule("GET", "/api/pairing/reach") is None


# ── Self rename / unpair, promote / limit ─────────────────────────────────────────────────


def test_a_device_can_rename_and_unpair_itself(client, devices):
    me, token = _phone(devices)
    resp = client.patch(f"/api/devices/{me['id']}", headers=_hdr(token), json={"name": "my phone"})
    assert resp.status_code == 200 and resp.json()["device"]["name"] == "my phone"
    resp = client.delete(f"/api/devices/{me['id']}", headers=_hdr(token))
    assert resp.status_code == 200
    assert devices.verify_token(token) is None


def test_a_device_cannot_rename_or_unpair_another(client, devices):
    other, other_token = _phone(devices, "tablet")
    _, token = _phone(devices)
    assert client.patch(f"/api/devices/{other['id']}", headers=_hdr(token), json={"name": "x"}).status_code == 403
    assert client.delete(f"/api/devices/{other['id']}", headers=_hdr(token)).status_code == 403
    assert devices.verify_token(other_token) is not None  # untouched


def test_self_exception_is_bare_path_only(client, devices):
    """A proxied ``/agents/<slug>/api/devices/<id>`` names a device in the MEMBER's registry —
    the hub's device id means nothing there, so no self-exception."""
    me, token = _phone(devices)
    assert client.delete(f"/agents/peer/api/devices/{me['id']}", headers=_hdr(token)).status_code == 403


def test_promote_requires_a_full_caller(client, devices):
    phone, _ = _phone(devices)
    assert phone["scope"] == "device"
    resp = client.patch(f"/api/devices/{phone['id']}", headers=_hdr(_SECRET), json={"scope": "full"})
    assert resp.status_code == 200 and resp.json()["device"]["scope"] == "full"
    resp = client.patch(f"/api/devices/{phone['id']}", headers=_hdr(_SECRET), json={"scope": "device"})
    assert resp.status_code == 200 and resp.json()["device"]["scope"] == "device"


def test_a_device_cannot_promote_itself(client, devices):
    me, token = _phone(devices)
    resp = client.patch(f"/api/devices/{me['id']}", headers=_hdr(token), json={"scope": "full"})
    assert resp.status_code == 403 and resp.json()["error"] == "device_scope"
    # …not even smuggled in with a rename.
    resp = client.patch(f"/api/devices/{me['id']}", headers=_hdr(token), json={"name": "x", "scope": "full"})
    assert resp.status_code == 403
    assert devices.verify_token(token).effective_scope() == "device"
    assert [d for d in devices.list_devices() if d["id"] == me["id"]][0]["name"] == "phone"


def test_a_device_cannot_promote_another(client, devices):
    other, _ = _phone(devices, "tablet")
    _, token = _phone(devices)
    resp = client.patch(f"/api/devices/{other['id']}", headers=_hdr(token), json={"scope": "full"})
    assert resp.status_code == 403


def test_an_agent_cannot_be_limited_and_bad_scopes_are_400(client, devices):
    hub, _ = _hub(devices)
    phone, _ = _phone(devices)
    assert client.patch(f"/api/devices/{hub['id']}", headers=_hdr(_SECRET), json={"scope": "device"}).status_code == 400
    for bad in ("admin", "", None, ["full"]):
        resp = client.patch(f"/api/devices/{phone['id']}", headers=_hdr(_SECRET), json={"scope": bad})
        assert resp.status_code == 400, bad
    assert client.patch(f"/api/devices/{phone['id']}", headers=_hdr(_SECRET), json={}).status_code == 400
    assert client.patch("/api/devices/nope", headers=_hdr(_SECRET), json={"scope": "full"}).status_code == 404


def test_a_promoted_phone_loses_the_ceiling(echo, devices):
    phone, token = _phone(devices)
    assert echo.post("/api/pairing/cancel", headers=_hdr(token)).status_code == 403
    devices.set_device_scope(phone["id"], "full")
    assert echo.post("/api/pairing/cancel", headers=_hdr(token)).status_code == 200


def test_devices_list_carries_scope_and_the_caller(client, devices):
    phone, token = _phone(devices)
    _hub(devices)
    body = client.get("/api/devices", headers=_hdr(token)).json()
    scopes = {d["kind"]: d["scope"] for d in body["devices"]}
    assert scopes == {"device": "device", "agent": "full"}
    assert body["caller"] == {"scope": "device", "device_id": phone["id"]}
    body = client.get("/api/devices", headers=_hdr(_SECRET)).json()
    assert body["caller"] == {"scope": "full", "device_id": None}


# ── Device routes: unknown ids, self vs other, promotion ──────────────────────────────────


def test_unknown_device_ids_are_404_for_a_full_caller(client, devices):
    phone, token = _phone(devices)
    before = devices.list_devices()
    assert client.delete("/api/devices/nope", headers=_hdr(_SECRET)).status_code == 404
    assert client.patch("/api/devices/nope", headers=_hdr(_SECRET), json={"name": "x"}).status_code == 404
    resp = client.patch("/api/devices/nope", headers=_hdr(_SECRET), json={"name": "x", "scope": "full"})
    assert resp.status_code == 404 and resp.json()["error"] == "unknown device"
    assert devices.list_devices() == before
    assert devices.verify_token(token).id == phone["id"]


def test_a_device_scope_caller_gets_403_not_404_for_an_unknown_id(client, devices):
    """The ceiling answers first for any id that isn't the caller's own, so a phone can't use
    the 404/403 difference to learn which device ids exist."""
    _, token = _phone(devices)
    assert client.delete("/api/devices/nope", headers=_hdr(token)).status_code == 403
    assert client.patch("/api/devices/nope", headers=_hdr(token), json={"name": "x"}).status_code == 403


def test_a_full_caller_revokes_another_device(client, devices):
    other, other_token = _phone(devices, "tablet")
    me, token = _phone(devices)
    assert client.delete(f"/api/devices/{other['id']}", headers=_hdr(_SECRET)).status_code == 200
    assert devices.verify_token(other_token) is None
    assert devices.verify_token(token).id == me["id"]  # only the named one
    assert client.delete(f"/api/devices/{other['id']}", headers=_hdr(_SECRET)).status_code == 404  # already gone


def test_a_promoted_phone_can_manage_other_devices(client, devices):
    other, _ = _phone(devices, "tablet")
    me, token = _phone(devices)
    assert client.patch(f"/api/devices/{other['id']}", headers=_hdr(token), json={"name": "x"}).status_code == 403
    devices.set_device_scope(me["id"], "full")
    resp = client.patch(f"/api/devices/{other['id']}", headers=_hdr(token), json={"scope": "full", "name": "tab"})
    assert resp.status_code == 200
    assert resp.json()["device"]["scope"] == "full" and resp.json()["device"]["name"] == "tab"


def test_a_bad_name_with_a_good_scope_changes_nothing(client, devices):
    phone, _ = _phone(devices)
    resp = client.patch(f"/api/devices/{phone['id']}", headers=_hdr(_SECRET), json={"scope": "full", "name": "\u202e"})
    assert resp.status_code == 400
    row = next(d for d in devices.list_devices() if d["id"] == phone["id"])
    assert row["scope"] == "device" and row["name"] == "phone"  # validated before either write


def test_a_device_may_read_reach(client, devices, monkeypatch):
    """``GET /api/pairing/reach`` is not on the ceiling: a phone sees how it can be reached."""
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_offer_hosts", lambda port: ([], {"available": False, "served": None}))
    monkeypatch.setattr(pr, "_pairable_addresses", lambda: [])
    _, token = _phone(devices)
    resp = client.get("/api/pairing/reach", headers=_hdr(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["hosts"] == [] and body["available"] == [] and body["auth_configured"] is True


# ── Trust tier is untouched ───────────────────────────────────────────────────────────────


def test_trust_tier_stays_operator_for_a_device(echo, devices):
    """Every ``== "operator"`` consumer (chat, goal trust, knowledge trust) keeps its meaning."""
    me, token = _phone(devices)
    body = echo.post("/api/chat", headers=_hdr(token), json={}).json()
    assert body["trust_tier"] == "operator"
    assert body["scope"] == "device"
    assert body["device_id"] == me["id"]


def test_bearer_tier_and_scope_for_each_credential(auth, devices, monkeypatch):
    _, phone = _phone(devices)
    _, hub = _hub(devices)
    auth.set_fleet_token("fleet-tok")
    try:
        assert auth.bearer_tier(phone) == "operator" and auth.bearer_scope(phone) == "device"
        assert auth.bearer_tier(hub) == "operator" and auth.bearer_scope(hub) == "full"
        assert auth.bearer_scope(_SECRET) == "full"
        assert auth.bearer_scope("fleet-tok") == "full"
        assert auth.bearer_scope("nope") is None
    finally:
        auth.set_fleet_token(None)


def test_x_api_key_is_full_scope(devices):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from a2a_impl import auth

    auth.configure(bearer_token="", api_key="legacy-key", allowed_origins_raw="")
    app = FastAPI()

    @app.post("/api/pairing/start")
    async def _s(request: Request):
        return {"scope": request.state.credential_scope}

    app.add_middleware(auth.A2AAuthMiddleware)
    resp = TestClient(app).post("/api/pairing/start", headers={"X-API-Key": "legacy-key"})
    assert resp.status_code == 200 and resp.json()["scope"] == "full"


# ── Registry: legacy rows, new pairings ───────────────────────────────────────────────────


def test_legacy_rows_read_as_full_and_are_not_rewritten(devices):
    token = "legacy-token"
    rows = [
        {
            "id": "abc",
            "name": "old phone",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": 1.0,
            "last_seen_at": time.time(),  # fresh, so verify's last-seen touch won't write
        }
    ]
    path = devices._registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), "utf-8")
    before = path.read_bytes()
    assert devices.list_devices()[0]["scope"] == "full"
    assert devices.verify_token(token).effective_scope() == "full"
    assert path.read_bytes() == before  # loading didn't migrate the row


def test_an_unknown_stored_scope_reads_as_device(devices):
    _, token = _phone(devices)
    path = devices._registry_path()
    rows = json.loads(path.read_text("utf-8"))
    rows[0]["scope"] = "superuser"
    path.write_text(json.dumps(rows), "utf-8")
    assert devices.verify_token(token).effective_scope() == "device"


def test_a_new_phone_pairing_is_device_scope(devices):
    device, token = _phone(devices)
    assert device["scope"] == "device"
    assert devices.verify_token(token).effective_scope() == "device"
    assert json.loads(devices._registry_path().read_text("utf-8"))[0]["scope"] == "device"


def test_an_agent_pairing_is_full_scope(devices):
    device, token = _hub(devices)
    assert device["scope"] == "full"
    assert devices.verify_token(token).effective_scope() == "full"


def test_an_agent_row_is_full_whatever_it_stores(devices):
    _, token = _hub(devices)
    path = devices._registry_path()
    rows = json.loads(path.read_text("utf-8"))
    rows[0]["scope"] = "device"
    path.write_text(json.dumps(rows), "utf-8")
    assert devices.verify_token(token).effective_scope() == "full"


# ── SSE tokens ────────────────────────────────────────────────────────────────────────────


def test_sse_token_minted_with_device_scope_verifies_as_device(auth):
    assert auth._sse_token_scope(auth.generate_sse_token("s", scope="device")) == "device"
    assert auth._sse_token_scope(auth.generate_sse_token("s", scope="full")) == "full"
    assert auth._validate_sse_token(auth.generate_sse_token("s", scope="device"))


def test_sse_token_defaults_to_the_narrow_scope(auth):
    assert auth._sse_token_scope(auth.generate_sse_token()) == "device"
    assert auth._sse_token_scope(auth.generate_sse_token(scope="bogus")) == "device"


def _legacy_sse_token(key: str) -> str:
    payload = json.dumps({"sid": "", "exp": int(time.time()) + 30}, separators=(",", ":")).encode()
    sig = hmac.new(key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + sig).decode()


def test_a_scopeless_pre_adr_sse_token_reads_as_device(auth):
    """Fail-safe: the old format can't say whether a phone minted it."""
    tok = _legacy_sse_token(_SECRET)
    assert auth._validate_sse_token(tok)
    assert auth._sse_token_scope(tok) == "device"


def test_scope_cannot_be_forged_into_an_sse_token(auth):
    tok = auth.generate_sse_token(scope="device")
    raw = base64.urlsafe_b64decode(tok.encode())
    forged = raw.replace(b'"scope":"device"', b'"scope":"full"')
    assert forged != raw
    assert auth._sse_token_scope(base64.urlsafe_b64encode(forged).decode()) is None


def test_the_sse_query_token_path_restores_the_scope(echo, auth):
    for scope in ("device", "full"):
        tok = auth.generate_sse_token(scope=scope)
        body = echo.get(f"/agents/peer/api/events?token={tok}").json()
        assert body["trust_tier"] == "operator" and body["scope"] == scope


@pytest.mark.parametrize(
    "path",
    [
        "/api/events",
        "/api/chat/attend",
        "/agents/peer/api/events",
        "/agents/peer/api/chat/attend",
        "/agents/hub-b/agents/peer/api/events",  # a hub of hubs: one proxy prefix per hop
    ],
)
def test_sse_query_token_paths_are_the_streams_bare_or_proxied(auth, path):
    assert auth._is_sse_query_token_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/foo/api/events",
        "/internal/api/chat/attend",
        "/agents/peer/x/api/events",  # not the /agents/<slug> form
        "/agents//api/events",  # empty slug
        "/agents/api/events",
        "/api/events/",
        "/api/events/extra",
        "/xapi/events",
        "/api/eventsx",
        "api/events",
        "",
    ],
)
def test_sse_query_token_paths_are_anchored(auth, path):
    """Exact path or ``/agents/<slug>`` + exact path — never a mere suffix (#4355)."""
    assert not auth._is_sse_query_token_path(path)


def test_a_suffix_lookalike_does_not_take_a_query_token(echo, auth):
    tok = auth.generate_sse_token(scope="full")
    assert echo.get(f"/foo/api/events?token={tok}").status_code == 401


def _sse_minting_app(auth):
    """The middleware + ``GET /api/sse-token`` answered by the server's own minting function
    (`auth.sse_token_for`), + an echo stream path that reports what the token restored."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/api/sse-token")
    async def _mint(request: Request):
        return {"token": auth.sse_token_for(request)}

    @app.get("/api/events")
    async def _events(request: Request):
        return {
            "scope": getattr(request.state, "credential_scope", None),
            "device_id": getattr(request.state, "device_id", None),
        }

    app.add_middleware(auth.A2AAuthMiddleware)
    return TestClient(app)


def test_an_sse_token_minted_by_a_phone_verifies_as_that_device(auth, devices):
    """Behaviour, not source: a paired phone mints a stream token, and the token verifies — and
    opens a stream — as ``device`` scope, stamped with that phone's id."""
    phone, token = _phone(devices)
    http = _sse_minting_app(auth)
    tok = http.get("/api/sse-token", headers=_hdr(token)).json()["token"]
    assert auth._sse_token_claims(tok) == ("device", phone["id"])
    assert http.get(f"/api/events?token={tok}").json() == {"scope": "device", "device_id": phone["id"]}


def test_an_sse_token_minted_by_the_shared_bearer_is_full(auth, devices):
    http = _sse_minting_app(auth)
    tok = http.get("/api/sse-token", headers=_hdr(_SECRET)).json()["token"]
    assert auth._sse_token_claims(tok) == ("full", None)
    # A phone that was PROMOTED mints full-scope tokens too: the scope is the minter's now.
    phone, token = _phone(devices)
    devices.set_device_scope(phone["id"], "full")
    tok = http.get("/api/sse-token", headers=_hdr(token)).json()["token"]
    assert auth._sse_token_claims(tok) == ("full", phone["id"])


def test_an_unstamped_request_mints_the_narrow_scope(auth):
    assert auth._sse_token_scope(auth.sse_token_for(SimpleNamespace(state=SimpleNamespace()))) == "device"


def test_the_server_route_mints_through_sse_token_for():
    """The server's ``/api/sse-token`` handler is the function tested above (parsed, so a
    docstring or reformat can't break this the way a fixed character window did)."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/server/__init__.py").read_text("utf-8"))
    handlers = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant) and d.args[0].value == "/api/sse-token"
            for d in fn.decorator_list
        )
    ]
    assert len(handlers) == 1
    calls = {n.func.attr for n in ast.walk(handlers[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "sse_token_for" in calls and "generate_sse_token" not in calls


# ── Config writes ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "auth.token",
        "auth.federation_token",
        "network.bind",
        "network.trusted_proxies",  # S7.1's key — covered by prefix the day it lands
        "network.public_console_url",  # S7.4's
        "devices.idle_ttl_days",  # S7.3's
        "egress.allowed_hosts",
        "filesystem.allow_run",
        "secrets_manager.host",
        "plugins.enabled",
        # Commands the server runs — host code that outlives a revoke.
        "mcp.servers",
        "mcp.enabled",
        "delegates",
        "lifecycle_hooks",
        "acp.agents",
        "filesystem.editor_command",
    ],
)
def test_credential_plane_keys_are_flagged(key):
    from graph.settings_schema import credential_plane_keys, is_credential_plane_key

    assert is_credential_plane_key(key)
    assert credential_plane_keys({key: "x"}) == [key]


@pytest.mark.parametrize("key", ["identity.name", "model.name", "filesystem.enabled", "tools.disabled", "networking"])
def test_ordinary_keys_are_not(key):
    from graph.settings_schema import is_credential_plane_key

    assert not is_credential_plane_key(key)


def test_nested_updates_are_flattened():
    from graph.settings_schema import credential_plane_keys

    assert credential_plane_keys({"identity": {"name": "x"}}, nested=True) == []
    assert credential_plane_keys({"auth": {"token": "x"}, "identity": {"name": "y"}}, nested=True) == ["auth.token"]
    assert credential_plane_keys({"network": {}}, nested=True) == ["network"]
    assert credential_plane_keys({"filesystem": {"allow_run": True}}, nested=True) == ["filesystem.allow_run"]
    assert credential_plane_keys(None, nested=True) == []


def test_flagged_fields_are_marked_in_the_schema():
    from graph.settings_schema import FIELDS

    flagged = {f.key for f in FIELDS if f.credential_plane}
    assert {"auth.token", "auth.federation_token", "network.bind", "egress.allowed_hosts", "filesystem.allow_run"} <= flagged


@pytest.fixture
def config_client(monkeypatch, auth):
    """The real config routes behind a stub that stamps the scope the test asks for."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.agent_init as agent_init
    from operator_api.config_routes import register_config_routes

    writes: list = []

    def _apply(config=None, soul=None, layer="agent"):
        writes.append(config)
        return True, ["saved"]

    monkeypatch.setattr(agent_init, "_apply_settings_changes", _apply)
    monkeypatch.setattr(
        "operator_api.config_routes._reset_settings_keys", lambda keys: (writes.append(keys) or True, ["reset"])
    )
    app = FastAPI()
    register_config_routes(app)

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.credential_scope = request.headers.get("x-test-scope", "full")
        return await call_next(request)

    c = TestClient(app)
    c.writes = writes
    return c


def test_device_scope_settings_write_of_a_plane_key_is_403_naming_it(config_client):
    resp = config_client.post(
        "/api/settings",
        headers={"x-test-scope": "device"},
        json={"updates": {"identity.name": "x", "network.bind": "0.0.0.0"}},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "device_scope" and body["keys"] == ["network.bind"]
    assert "network.bind" in body["detail"]["reason"]
    assert config_client.writes == []  # nothing applied — not even the harmless key


def test_device_scope_can_still_write_ordinary_keys(config_client):
    resp = config_client.post("/api/settings", headers={"x-test-scope": "device"}, json={"updates": {"identity.name": "x"}})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert config_client.writes


def test_full_scope_can_write_plane_keys(config_client):
    resp = config_client.post("/api/settings", headers={"x-test-scope": "full"}, json={"updates": {"network.bind": "0.0.0.0"}})
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_device_scope_reset_of_a_plane_key_is_403(config_client):
    resp = config_client.post("/api/settings/reset", headers={"x-test-scope": "device"}, json={"keys": ["auth.token"]})
    assert resp.status_code == 403 and resp.json()["keys"] == ["auth.token"]


def test_device_scope_nested_config_write_is_checked(config_client):
    resp = config_client.post(
        "/api/config", headers={"x-test-scope": "device"}, json={"config": {"auth": {"token": "mine"}}}
    )
    assert resp.status_code == 403 and resp.json()["keys"] == ["auth.token"]
    resp = config_client.post("/api/config", headers={"x-test-scope": "device"}, json={"config": {"identity": {"name": "x"}}})
    assert resp.status_code == 200


def test_device_scope_setup_finish_is_checked(config_client):
    resp = config_client.post(
        "/api/config/setup", headers={"x-test-scope": "device"}, json={"config": {"network": {"bind": "0.0.0.0"}}, "soul": ""}
    )
    assert resp.status_code == 403 and resp.json()["keys"] == ["network.bind"]


def test_an_unstamped_scope_on_a_secured_instance_is_narrow(auth):
    """No middleware ran but a credential is configured ⇒ treat as device, never full."""
    from operator_api.scope_guard import credential_plane_refusal

    req = SimpleNamespace(state=SimpleNamespace())
    assert credential_plane_refusal(req, {"auth.token": "x"}) is not None
    assert credential_plane_refusal(req, {"identity.name": "x"}) is None


# ── Fleet proxy: the ceiling runs BEFORE the swap ─────────────────────────────────────────


def test_ws_query_token_swap_refuses_a_device_on_a_plane_path_before_swapping(auth, devices, monkeypatch):
    from graph.fleet import proxy

    _, token = _phone(devices)

    def _no_swap():
        raise AssertionError("the fleet token must not be resolved for a refused request")

    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", _no_swap)
    q, allowed = proxy._member_ws_query("alice", f"token={token}", "api/pairing/start")
    assert not allowed and "fleet" not in q
    # The host slug too — the check runs before the host pass-through.
    _, allowed = proxy._member_ws_query("host", f"token={token}", "api/plugin-setup/x/y")
    assert not allowed


def test_ws_query_token_swap_still_happens_for_a_device_elsewhere(auth, devices, monkeypatch):
    from urllib.parse import parse_qs

    from graph.fleet import proxy

    _, token = _phone(devices)
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-tok")
    q, allowed = proxy._member_ws_query("alice", f"token={token}&cols=80", "plugins/terminal/pty")
    assert allowed and parse_qs(q)["token"] == ["fleet-tok"]


def test_remote_ws_handshake_refuses_a_device_on_a_plane_path(auth, devices):
    from graph.fleet import proxy

    _, token = _phone(devices)
    _, headers, refusal = proxy._remote_ws_handshake(f"token={token}", None, "stored", "api/secrets/sync")
    assert refusal == "device_scope" and headers == {}
    _, headers, refusal = proxy._remote_ws_handshake("", f"Bearer {token}", "stored", "api/secrets/sync")
    assert refusal == "device_scope"
    q, _, refusal = proxy._remote_ws_handshake(f"token={token}", None, "stored", "plugins/terminal/pty")
    assert refusal is None and "stored" in q


@pytest.mark.asyncio
async def test_http_proxy_refuses_before_the_swap(monkeypatch):
    from graph.fleet import proxy

    def _no_swap():
        raise AssertionError("swap must not run")

    async def _no_forward(*a, **k):
        raise AssertionError("must not forward")

    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", _no_swap)
    monkeypatch.setattr(proxy, "_forward_to_base", _no_forward)
    req = SimpleNamespace(
        method="POST",
        headers={},
        state=SimpleNamespace(trust_tier="operator", credential_scope="device", member_public=False),
    )
    resp = await proxy._forward_member("alice", req, "api/plugins/install", "api/plugins/install", ("http://m", {}))
    assert resp.status_code == 403
    assert json.loads(resp.body)["error"] == "device_scope"


@pytest.mark.asyncio
async def test_http_proxy_still_swaps_for_a_device_elsewhere(monkeypatch):
    from graph.fleet import proxy

    seen = {}

    async def _forward(base, request, raw_path, extra, **k):
        seen["extra"] = extra
        return "forwarded"

    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "fleet-tok")
    monkeypatch.setattr(proxy, "_forward_to_base", _forward)
    req = SimpleNamespace(
        method="POST",
        headers={},
        state=SimpleNamespace(trust_tier="operator", credential_scope="device", member_public=False),
    )
    assert await proxy._forward_member("alice", req, "api/chat", "api/chat", ("http://m", {})) == "forwarded"
    assert seen["extra"]["authorization"] == "Bearer fleet-tok"


def test_an_encoded_query_or_fragment_char_cannot_hide_a_ceiling_path(echo, devices):
    """``%3F``/``%23`` decode INTO the routed path but cut ``request.url.path`` short — the
    ceiling checks both spellings."""
    _, token = _phone(devices)
    for path in ("/agents/peer/api/plugins/install%3Fx", "/agents/peer/api/secrets/sync%23", "/api/plugin-setup/a%23/b"):
        assert echo.post(path, headers=_hdr(token)).status_code == 403, path


def test_mcp_server_writes_are_on_the_ceiling(echo, devices):
    """An MCP server is a command the server spawns: adding or changing one leaves host code
    behind that outlives a revoke (the plugin-lifecycle reason)."""
    from a2a_impl.credential_scope import ceiling_rule

    _, token = _phone(devices)
    for method, path in [
        ("POST", "/api/mcp/servers"),
        ("POST", "/api/mcp/servers/import"),
        ("POST", "/api/mcp/servers/fs/promote"),
        ("POST", "/api/mcp/servers/fs/forget"),
        ("DELETE", "/api/mcp/servers/fs"),
        ("PATCH", "/api/mcp/servers/fs"),
        ("PUT", "/agents/peer/api/mcp/servers/fs"),
    ]:
        assert ceiling_rule(method, path) is not None, (method, path)
        assert echo.request(method, path, headers=_hdr(token), json={}).status_code == 403, (method, path)
    # Reads stay open.
    for path in ("/api/mcp/catalog", "/api/mcp/exposed"):
        assert echo.get(path, headers=_hdr(token)).status_code == 200


def test_device_scope_cannot_add_commands_through_config(config_client):
    for config, key in [
        ({"mcp": {"servers": [{"name": "x", "command": "sh"}]}}, "mcp.servers"),
        ({"delegates": [{"name": "d", "command": "sh"}]}, "delegates"),
        ({"lifecycle_hooks": [{"event": "turn_end", "command": "sh"}]}, "lifecycle_hooks"),
        ({"acp": {"agents": {"evil": {"command": "sh"}}}}, "acp.agents.evil.command"),
    ]:
        resp = config_client.post("/api/config", headers={"x-test-scope": "device"}, json={"config": config})
        assert resp.status_code == 403 and resp.json()["keys"] == [key], config
    resp = config_client.post(
        "/api/settings", headers={"x-test-scope": "device"}, json={"updates": {"filesystem.editor_command": "sh -c x"}}
    )
    assert resp.status_code == 403 and resp.json()["keys"] == ["filesystem.editor_command"]


def test_device_scope_cannot_set_the_public_console_url(config_client):
    """S7.4: declaring the console public decides who can reach the sign-in — a phone can't."""
    resp = config_client.post(
        "/api/settings",
        headers={"x-test-scope": "device"},
        json={"updates": {"network.public_console_url": "https://agent.example.com"}},
    )
    assert resp.status_code == 403 and resp.json()["keys"] == ["network.public_console_url"]
    resp = config_client.post(
        "/api/config",
        headers={"x-test-scope": "device"},
        json={"config": {"network": {"public_console_url": "https://agent.example.com"}}},
    )
    assert resp.status_code == 403 and resp.json()["keys"] == ["network.public_console_url"]
    assert config_client.writes == []
