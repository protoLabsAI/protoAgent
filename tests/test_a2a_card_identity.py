"""A2A card identity is config/plugin-driven (#570) — a fork declares its
advertised skills + card description in langgraph-config.yaml or via a plugin's
register_a2a_skill, with ZERO edits to server/a2a.py. The template default
(one free-text "chat" skill) holds when nothing is provided."""

import server
import server.a2a as a2a
from graph.config import LangGraphConfig


# ── config parse ────────────────────────────────────────────────────────────


def test_config_parses_a2a_section(tmp_path):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(
        "a2a:\n  description: Acme triage bot\n  skills:\n    - id: triage\n      name: Triage\n      description: d\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.a2a_description == "Acme triage bot"
    assert [s["id"] for s in cfg.a2a_skills] == ["triage"]


def test_config_a2a_defaults_empty(tmp_path):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("model:\n  provider: openai\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.a2a_skills == [] and cfg.a2a_description == ""


# ── plugin hook ─────────────────────────────────────────────────────────────


def test_register_a2a_skill_accumulates_and_validates():
    from graph.plugins.registry import PluginRegistry

    reg = PluginRegistry.__new__(PluginRegistry)  # avoid HOST import in __init__
    reg.plugin_id = "demo"
    reg.a2a_skills = []
    reg.register_a2a_skill({"id": "s1", "name": "S1", "description": "d"})
    reg.register_a2a_skill({"name": "no-id"})  # rejected: no id
    reg.register_a2a_skill("not-a-dict")  # rejected: not a dict
    assert [s["id"] for s in reg.a2a_skills] == ["s1"]


# ── resolver precedence: config + plugin, else default ──────────────────────


def _cfg(skills=None, description=""):
    c = LangGraphConfig()
    c.a2a_skills = skills or []
    c.a2a_description = description
    return c


def test_resolver_falls_back_to_template_default(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    assert [s["id"] for s in a2a._resolved_skill_specs()] == ["chat"]  # the placeholder


def test_resolver_uses_config_then_plugin(monkeypatch):
    monkeypatch.setattr(
        server.STATE, "graph_config", _cfg(skills=[{"id": "cfg1", "name": "Cfg1", "description": "d"}]), raising=False
    )
    monkeypatch.setattr(
        server.STATE, "plugin_a2a_skills", [{"id": "plug1", "name": "Plug1", "description": "d"}], raising=False
    )
    ids = [s["id"] for s in a2a._resolved_skill_specs()]
    assert ids == ["cfg1", "plug1"]  # config first, then plugins; default dropped


def test_agent_skills_built_from_resolver(monkeypatch):
    monkeypatch.setattr(
        server.STATE,
        "graph_config",
        _cfg(
            skills=[{"id": "triage", "name": "Triage", "description": "d", "result_mime": "application/vnd.x-v1+json"}]
        ),
        raising=False,
    )
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    skills = a2a._agent_skills()
    assert skills[0].id == "triage"
    assert list(skills[0].output_modes) == ["application/vnd.x-v1+json"]
    # structured schema resolves through the same source
    monkeypatch.setattr(
        server.STATE,
        "graph_config",
        _cfg(
            skills=[
                {
                    "id": "triage",
                    "name": "T",
                    "description": "d",
                    "result_mime": "application/vnd.x-v1+json",
                    "output_schema": {"type": "object"},
                }
            ]
        ),
        raising=False,
    )
    assert a2a.structured_skill_schema("triage") == {"schema": {"type": "object"}, "mime": "application/vnd.x-v1+json"}


# ── card description from config, default otherwise ─────────────────────────


def test_card_description_from_config(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config", _cfg(description="Acme triage bot"), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(server.STATE, "active_port", 7870, raising=False)
    card = a2a._build_agent_card_proto()
    assert card.description == "Acme triage bot"


def test_card_description_default(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config", _cfg(description=""), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(server.STATE, "active_port", 7870, raising=False)
    card = a2a._build_agent_card_proto()
    assert card.description == a2a._DEFAULT_CARD_DESCRIPTION


# ── protocol-version advertisement (A2A version negotiation) ─────────────────


def test_card_dict_carries_protocol_version_hint(monkeypatch):
    """The served card JSON exposes a top-level, proto-free protocolVersion hint —
    so a delegating peer can pre-check compatibility without walking the proto
    supportedInterfaces — and it AGREES with the native interface field."""
    monkeypatch.setattr(server.STATE, "graph_config", _cfg(), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(server.STATE, "active_port", 7870, raising=False)
    d = a2a.agent_card_dict(a2a._build_agent_card_proto())
    assert d["protocolVersion"] == "1.0"
    assert d["supportedVersions"] == ["1.0"]
    # native proto field stays populated and agrees with the proto-free hint
    jsonrpc = next(i for i in d["supportedInterfaces"] if i["protocolBinding"] == "JSONRPC")
    assert jsonrpc["protocolVersion"] == d["protocolVersion"] == a2a.A2A_PROTOCOL_VERSION


async def test_agent_card_route_serves_protocol_version(monkeypatch):
    """The /.well-known/agent-card.json route serves the protocol-version hint."""
    import json

    monkeypatch.setattr(server.STATE, "graph_config", _cfg(), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(server.STATE, "active_port", 7870, raising=False)
    routes = a2a.agent_card_routes(a2a._build_agent_card_proto())
    assert routes and routes[0].path == "/.well-known/agent-card.json"
    resp = await routes[0].endpoint(None)  # request is unused
    body = json.loads(resp.body)
    assert body["protocolVersion"] == "1.0"
    assert body["supportedVersions"] == ["1.0"]


# ── the card must describe the guard, not a parallel guess (#2620) ──────────


def _card(bearer, api_key):
    """Build a card under a given auth configuration and return (schemes, requirements)."""
    import protolabs_a2a as pa

    from a2a_impl import auth

    auth.configure(bearer_token=bearer, api_key=api_key, allowed_origins_raw="")
    card = pa.build_agent_card(
        name="x", description="d", url="http://h/a2a", version="1", skills=[], bearer=bool(bearer)
    )
    a2a._apply_real_security(card, pa)
    return sorted(card.security_schemes), [sorted(r.schemes) for r in card.security_requirements]


def test_bearer_only_agent_does_not_advertise_the_api_key_scheme():
    """The reported bug: a bearer-gated agent advertised X-API-Key as a valid
    alternative. A standards-following A2A client is entitled to pick the advertised
    apiKey scheme — and got 401 from a healthy, correctly configured agent."""
    schemes, reqs = _card("tok", "")

    assert schemes == ["bearer"]
    assert reqs == [["bearer"]]


def test_api_key_only_agent_advertises_only_the_api_key_scheme():
    assert _card(None, "key") == (["apiKey"], [["apiKey"]])


def test_both_configured_is_advertised_as_two_alternatives():
    """With both configured, EITHER credential authenticates, so the card lists two
    requirements — a set of alternatives.

    This assertion was briefly the opposite. The guard used to check the two credentials
    sequentially, making them an accidental AND, and the first cut of this fix faithfully
    documented that on the card. The conjunction was never a decision (see
    `auth._classify_request`), so the semantics were corrected instead of enshrined —
    naming both schemes inside ONE requirement would now be the lie."""
    schemes, reqs = _card("tok", "key")

    assert schemes == ["apiKey", "bearer"]
    assert sorted(reqs) == [["apiKey"], ["bearer"]], "one requirement each = alternatives"


def test_open_mode_advertises_no_credential_at_all():
    """An open endpoint claiming to want a credential is its own small lie."""
    assert _card(None, "") == ([], [])


def test_enforced_schemes_reports_the_guard_s_RESOLVED_state(monkeypatch):
    """The root cause was two derivations of one fact: the card re-derived "is a bearer
    configured?" from config + env, while enforcement used whatever `configure()` resolved.

    `enforced_schemes` reads the guard, so the card follows the SAME resolution — including
    the documented env fallback (`bearer_token=None` means "unspecified, use
    A2A_AUTH_TOKEN"). That is the property that stops the two drifting again."""
    from a2a_impl.auth import configure, enforced_schemes

    monkeypatch.setenv("A2A_AUTH_TOKEN", "from-env")
    configure(bearer_token=None, api_key="", allowed_origins_raw="")
    assert enforced_schemes() == (True, False)  # env fallback resolved → bearer IS enforced

    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    configure(bearer_token=None, api_key="key", allowed_origins_raw="")
    assert enforced_schemes() == (False, True)  # nothing to fall back to → api key only


def test_card_security_matches_what_the_middleware_accepts():
    """End-to-end: for each configuration, every credential the card advertises is one
    the middleware actually accepts — asserted against real requests, not a reading of
    the code."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from a2a_impl import auth

    for bearer, api_key in (("tok", ""), (None, "key"), ("tok", "key")):
        schemes, reqs = _card(bearer, api_key)
        app = FastAPI()

        @app.get("/a2a/ping")
        async def ping():
            return {"ok": True}

        auth.install(app, bearer_token=bearer, api_key=api_key, allowed_origins_raw="")
        headers = {}
        for name in reqs[0]:  # the single requirement = every credential that must be sent
            if name == "bearer":
                headers["Authorization"] = f"Bearer {bearer}"
            else:
                headers["x-api-key"] = api_key

        assert TestClient(app).get("/a2a/ping", headers=headers).status_code == 200, (
            f"card advertises {reqs[0]} for bearer={bool(bearer)}/api_key={bool(api_key)}, "
            "but sending exactly that was rejected"
        )
