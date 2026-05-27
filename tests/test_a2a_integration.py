"""Integration tests for the A2A port.

Locks the pieces that the pure handler tests can't see:
- _build_agent_card advertises streaming + pushNotifications (so
  @a2a-js/sdk clients switch to the async/streaming path).
- The card's ``url`` field still points at /a2a for JSON-RPC (regression
  guard — inline builds have omitted the /a2a suffix in the past,
  which makes @a2a-js/sdk POST to / and get a 405 from FastAPI).
- The cost-v1 extension declaration stays on the card so A2A consumers
  know to extract the emitted DataPart.

Forks should extend this file with tests for their own skills +
declared extensions.
"""

from __future__ import annotations


def test_agent_card_advertises_async_capabilities() -> None:
    """Without these flags, @a2a-js/sdk silently falls back to the
    synchronous blocking path and consumers would wait on the HTTP
    response for the full LangGraph run."""
    from server import _build_agent_card

    card = _build_agent_card("protoagent:7870")
    caps = card["capabilities"]
    assert caps["streaming"] is True
    assert caps["pushNotifications"] is True


def test_agent_card_url_points_at_rpc_endpoint() -> None:
    """The card's `url` field must target the JSON-RPC path, not the
    server root. @a2a-js/sdk sends message/send to whatever `url`
    says — a misplaced / gets 405 from FastAPI and nothing works."""
    from server import _build_agent_card

    card = _build_agent_card("protoagent:7870")
    assert card["url"].endswith("/a2a")


def test_agent_card_has_at_least_one_skill() -> None:
    """A card with zero skills is not usable by any planner. The
    template ships with a single placeholder `chat` skill so a fresh
    clone can actually be called. Forks should replace it with real
    skills — this test just locks in that the list is never empty."""
    from server import _build_agent_card

    card = _build_agent_card("protoagent:7870")
    skills = card.get("skills", [])
    assert skills, "agent card must declare at least one skill"
    for skill in skills:
        assert "id" in skill
        assert "name" in skill
        assert "description" in skill


def test_agent_card_no_bearer_when_token_unset(monkeypatch) -> None:
    """With A2A_AUTH_TOKEN unset, card must NOT advertise bearer scheme."""
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    from server import _build_agent_card

    card = _build_agent_card("protoagent:7870")
    schemes = card.get("securitySchemes", {})
    assert "apiKey" in schemes, "apiKey scheme must always be present"
    assert "bearer" not in schemes, "bearer must not appear when A2A_AUTH_TOKEN is unset"


def test_agent_card_bearer_when_token_set(monkeypatch) -> None:
    """With A2A_AUTH_TOKEN set, card must advertise bearer scheme."""
    monkeypatch.setenv("A2A_AUTH_TOKEN", "secret-test-token")
    from server import _build_agent_card

    card = _build_agent_card("protoagent:7870")
    schemes = card.get("securitySchemes", {})
    assert "apiKey" in schemes, "apiKey scheme must always be present"
    assert "bearer" in schemes, "bearer must appear when A2A_AUTH_TOKEN is set"
    assert schemes["bearer"] == {"type": "http", "scheme": "bearer"}


def test_agent_card_declares_cost_v1_extension() -> None:
    """The runtime captures token usage on `on_chat_model_end` and the
    A2A handler emits a cost-v1 DataPart on every terminal task. The
    extension must be declared on the card so A2A consumers (e.g.
    Workstacean's A2AExecutor) know to extract that DataPart onto
    ``result.data``, where the cost interceptor records per-skill
    samples for the fleet dashboard."""
    from server import _build_agent_card

    card = _build_agent_card("protoagent:7870")
    exts = card["capabilities"].get("extensions", [])
    cost_ext = next(
        (e for e in exts
         if e.get("uri") == "https://proto-labs.ai/a2a/ext/cost-v1"),
        None,
    )
    assert cost_ext is not None, (
        "Missing cost-v1 extension declaration — A2A consumers won't know "
        "to extract the emitted cost DataPart."
    )
