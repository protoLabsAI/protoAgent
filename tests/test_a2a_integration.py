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
- auth schemes (only the credentials the guard actually enforces — #2620).

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


def _configure_auth(bearer=None, api_key=""):
    """Configure the guard the way boot does — auth.install() runs BEFORE the card is
    built (server/__init__.py), and since #2620 the card is derived from the guard's
    resolved state rather than re-reading env itself."""
    from a2a_impl import auth

    auth.configure(bearer_token=bearer, api_key=api_key, allowed_origins_raw="")


def test_agent_card_no_bearer_when_token_unset(monkeypatch) -> None:
    """#2620 changed what "no token" advertises. These three tests used to assert
    'apiKey scheme must always be present' — encoding the defect as a requirement. An
    agent with no credential configured accepts anything, so advertising X-API-Key told
    clients to send a credential the server neither wants nor validates."""
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    import server

    monkeypatch.setattr(server.STATE, "graph_config", None, raising=False)
    _configure_auth(bearer=None, api_key="")

    schemes = _card_json().get("securitySchemes", {})
    assert "bearer" not in schemes, "bearer must not appear when no token is configured"
    assert "apiKey" not in schemes, "nor apiKey — the server enforces neither (#2620)"


def test_agent_card_bearer_when_token_set(monkeypatch) -> None:
    """Was: 'the security requirement must also list bearer as an OR alternative', with
    apiKey alongside it. Both halves were wrong — the server enforces bearer only, and a
    client picking the advertised apiKey got 401 (#2620)."""
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret-test-token")
    import server

    monkeypatch.setattr(server.STATE, "graph_config", None, raising=False)
    _configure_auth(bearer="secret-test-token", api_key="")

    card = _card_json()
    schemes = card.get("securitySchemes", {})
    assert "bearer" in schemes
    assert schemes["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert "apiKey" not in schemes, "no API key is configured — advertising it is the bug"
    scheme_keys = [set(r.get("schemes", {}).keys()) for r in card.get("securityRequirements", [])]
    assert scheme_keys == [{"bearer"}]


def test_agent_card_lists_each_configured_credential_as_an_alternative(monkeypatch) -> None:
    """With BOTH configured, either credential authenticates (#2620 flipped the guard's
    accidental AND to a deliberate OR), so the card lists one requirement per scheme."""
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret-test-token")
    import server

    monkeypatch.setattr(server.STATE, "graph_config", None, raising=False)
    _configure_auth(bearer="secret-test-token", api_key="legacy-key")

    card = _card_json()
    assert set(card.get("securitySchemes", {})) == {"apiKey", "bearer"}
    scheme_keys = sorted(set(r.get("schemes", {}).keys()) for r in card.get("securityRequirements", []))
    assert scheme_keys == [{"apiKey"}, {"bearer"}]


def test_agent_card_declares_exactly_the_extensions_it_emits() -> None:
    """The card is a contract, not a vocabulary dump.

    ``pa.ALL_EXTENSION_URIS`` is the fleet-wide vocabulary; this agent declares
    only the subset it actually emits (cost / worldstate-delta / tool-call).
    ``confidence-v1`` is the case that motivated this: it was declared for
    months while nothing ever emitted it, so a consumer building a calibration
    pipeline on the declaration would wait forever for a payload. Declaring an
    extension without an emission path is a broken promise a peer cannot detect
    except by timing out — so this asserts equality, not containment.
    """
    import protolabs_a2a as pa

    exts = _card_json()["capabilities"].get("extensions", [])
    declared = {e.get("uri") for e in exts}
    assert declared == {pa.COST_EXT_URI, pa.WORLDSTATE_DELTA_EXT_URI, pa.TOOL_CALL_EXT_URI}
    assert pa.CONFIDENCE_EXT_URI not in declared


def test_agent_card_declares_cost_v1_extension() -> None:
    """cost-v1 specifically — a consumer's cost interceptor engages on this
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
    import server.a2a

    mime = "application/vnd.protolabs.market-review-v1+json"
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
    # _SKILL_SPECS lives in server.a2a (ADR 0023 phase 2); _agent_skills /
    # structured_skill_schema read it from there, so patch it at its home.
    monkeypatch.setattr(
        server.a2a,
        "_SKILL_SPECS",
        [
            {
                "id": "market_review",
                "name": "Market Review",
                "description": "d",
                "tags": [],
                "examples": [],
                "output_schema": schema,
                "result_mime": mime,
            }
        ],
    )

    skill = MessageToDict(server._agent_skills()[0])
    assert skill["outputModes"] == [mime]  # advertised on the card
    got = server.structured_skill_schema("market_review")  # schema for the executor
    assert got == {"schema": schema, "mime": mime}

    # A skill with no schema → free text (no output_modes, no lookup).
    monkeypatch.setattr(
        server.a2a,
        "_SKILL_SPECS",
        [
            {
                "id": "chat",
                "name": "Chat",
                "description": "d",
                "tags": [],
                "examples": [],
            }
        ],
    )
    assert "outputModes" not in MessageToDict(server._agent_skills()[0])
    assert server.structured_skill_schema("chat") is None
