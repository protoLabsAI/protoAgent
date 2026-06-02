"""Integration tests for the A2A 1.0 port.

Locks the agent card that ``server._build_agent_card_proto`` produces (a
``a2a-sdk`` proto ``AgentCard`` built via ``protolabs_a2a.build_agent_card``),
serialized to the 1.0 wire JSON the SDK serves at
``/.well-known/agent-card.json``:

- capabilities advertise streaming + pushNotifications (so SDK clients switch
  to the async/streaming path).
- the JSONRPC ``supportedInterfaces`` entry points at /a2a, protocol 1.0
  (regression guard — a misplaced url makes clients POST to / and 405).
- the four protoLabs custom extensions are declared so consumers extract them.
- provider is the fleet provider block.
- auth schemes (apiKey always; bearer when a token is configured).

Forks should extend this with tests for their own skills + extensions.
"""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict


def _card_json(monkeypatch=None, *, bearer_token: str | None = None) -> dict:
    from server import _build_agent_card_proto

    # The interface url derives from A2A_PUBLIC_URL or the bound port (no host
    # arg) — _build_agent_card_proto() takes no parameters.
    card = _build_agent_card_proto()
    return MessageToDict(card, preserving_proto_field_name=False)


def test_agent_card_advertises_async_capabilities() -> None:
    caps = _card_json()["capabilities"]
    assert caps["streaming"] is True
    assert caps["pushNotifications"] is True


def test_agent_card_jsonrpc_interface_points_at_rpc_endpoint() -> None:
    """The JSONRPC interface url must target the /a2a path (protocol 1.0)."""
    card = _card_json()
    ifaces = card["supportedInterfaces"]
    jsonrpc = next(i for i in ifaces if i["protocolBinding"] == "JSONRPC")
    assert jsonrpc["url"].endswith("/a2a")
    assert jsonrpc["protocolVersion"] == "1.0"


def test_agent_card_url_honors_public_url_env(monkeypatch) -> None:
    """A2A_PUBLIC_URL overrides the interface url (deployed agents advertise
    their real external base, not the bound loopback port)."""
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://gina.example.com/")
    card = _card_json()
    jsonrpc = next(i for i in card["supportedInterfaces"] if i["protocolBinding"] == "JSONRPC")
    assert jsonrpc["url"] == "https://gina.example.com/a2a"


def test_agent_card_provider_is_fleet_provider() -> None:
    provider = _card_json()["provider"]
    assert provider["organization"] == "protoLabs AI"
    assert provider["url"] == "https://protolabs.ai"


def test_agent_card_has_at_least_one_skill() -> None:
    skills = _card_json().get("skills", [])
    assert skills, "agent card must declare at least one skill"
    for skill in skills:
        assert "id" in skill
        assert "name" in skill
        assert "description" in skill


def test_agent_card_no_bearer_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    import graph.config  # noqa: F401
    import server

    # No configured graph → _bearer_configured() falls back to env (unset).
    monkeypatch.setattr(server, "_graph_config", None, raising=False)
    schemes = _card_json().get("securitySchemes", {})
    assert "apiKey" in schemes, "apiKey scheme must always be present"
    assert "bearer" not in schemes, "bearer must not appear when no token is configured"


def test_agent_card_bearer_when_token_set(monkeypatch) -> None:
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret-test-token")
    import server

    monkeypatch.setattr(server, "_graph_config", None, raising=False)
    card = _card_json()
    schemes = card.get("securitySchemes", {})
    assert "apiKey" in schemes, "apiKey scheme must always be present"
    assert "bearer" in schemes, "bearer must appear when A2A_AUTH_TOKEN is set"
    assert schemes["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    # The security requirement must also list bearer as an OR alternative.
    reqs = card.get("securityRequirements", [])
    scheme_keys = [set(r.get("schemes", {}).keys()) for r in reqs]
    assert {"apiKey"} in scheme_keys and {"bearer"} in scheme_keys


def test_agent_card_security_requirement_apikey_only_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    import server

    monkeypatch.setattr(server, "_graph_config", None, raising=False)
    reqs = _card_json().get("securityRequirements", [])
    scheme_keys = [set(r.get("schemes", {}).keys()) for r in reqs]
    assert scheme_keys == [{"apiKey"}]


def test_agent_card_declares_all_four_protolabs_extensions() -> None:
    """The runtime emits cost / confidence / worldstate-delta / tool-call
    DataParts; every extension must be declared so A2A consumers know to
    extract them."""
    import protolabs_a2a as pa

    exts = _card_json()["capabilities"].get("extensions", [])
    declared = {e.get("uri") for e in exts}
    for uri in pa.ALL_EXTENSION_URIS:
        assert uri in declared, f"missing extension declaration: {uri}"


def test_agent_card_declares_cost_v1_extension() -> None:
    """cost-v1 specifically — Workstacean's cost interceptor engages on this
    canonical URI."""
    import protolabs_a2a as pa

    exts = _card_json()["capabilities"].get("extensions", [])
    assert any(e.get("uri") == pa.COST_EXT_URI for e in exts)


# ── structured-skill declaration (ADR-0006 addendum / #476, protoAgent side) ──


def test_structured_skill_advertises_mime_and_exposes_schema(monkeypatch) -> None:
    """A skill declaring output_schema + result_mime advertises the MIME in the
    card's output_modes, and structured_skill_schema() hands the executor the
    schema to enforce. Free-text skills stay None (default)."""
    import server

    mime = "application/vnd.protolabs.market-review-v1+json"
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
    monkeypatch.setattr(server, "_SKILL_SPECS", [{
        "id": "market_review", "name": "Market Review", "description": "d",
        "tags": [], "examples": [], "output_schema": schema, "result_mime": mime,
    }])

    skill = MessageToDict(server._agent_skills()[0])
    assert skill["outputModes"] == [mime]                 # advertised on the card
    got = server.structured_skill_schema("market_review")  # schema for the executor
    assert got == {"schema": schema, "mime": mime}

    # A skill with no schema → free text (no output_modes, no lookup).
    monkeypatch.setattr(server, "_SKILL_SPECS", [{
        "id": "chat", "name": "Chat", "description": "d", "tags": [], "examples": [],
    }])
    assert "outputModes" not in MessageToDict(server._agent_skills()[0])
    assert server.structured_skill_schema("chat") is None
