"""P1 proof K1: the A2A privacy proofs hold for every KIND of caller, not one identity.

The proof matrix in ``docs/design/conversations-in-work-proposal.md`` ("Proof that turns
REVISE into CONTINUE") requires each identity-sensitive scenario to run "for federation,
fleet-service, paired-agent and enrolled-console callers; no operator-tier shortcut." The
Phase-0/P1b scenarios in ``tests/test_a2a_hitl_parked_routing.py`` each prove privacy with
ONE hand-set caller, so no pass may rest on that single identity.

This file RE-RUNS six of those scenarios without copying their bodies: it imports each source
test and, for every caller kind, routes the source's inline ``ServerCallContext(state=...)``
constructions to principals built through the REAL auth path — never a hand-set tier:

* the three PEER kinds come straight through ``a2a_impl.auth.A2AAuthMiddleware`` exactly as
  ``tests/test_a2a_auth.py::test_verified_principal_in_http_call_context`` classifies them —
  ``federation`` (a federation bearer → ``trust_tier="federation"``), ``fleet-service`` (the
  ADR 0089 service token) and ``paired-agent`` (an ``agent``-kind device pairing →
  ``agent-device``). All three are NON-human (``auth.is_human_operator`` is False), which is
  the whole point of "no operator-tier shortcut".
* the ``enrolled-console`` kind is the human operator, reached the only way a human's authority
  crosses a trust hop — a verified ``fleet-proxy`` delegation (ADR 0131 P1a.2,
  ``graph.fleet.proxy.mint_member_delegation`` → ``auth.resolve_human_operator``). It carries the
  private content as the OPERATOR in EVERY run, so the operator tier is never hand-set; its own
  param run pairs that delegated operator with a representative (federation) peer.

Original assertions are kept verbatim — the source bodies are imported and awaited unchanged.
Where a scenario's ORIGINAL assertion fails for an operator-tier peer because the leak is real,
that case is a strict ``xfail`` named "PROOF FAILURE" (see the PR body's ``## Proof failures``) —
never fixed here, and never in ``src/``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import a2a.server.context as _scc
from a2a.server.context import ServerCallContext
from a2a.types import TaskState

from a2a_impl import auth
from a2a_impl.stores import tier_context_builder
from security import devices

from tests import test_a2a_hitl_parked_routing as src
from tests.test_a2a_hitl_parked_routing import _drain, _hooks  # noqa: F401 — re-export autouse fixtures

# The auth classifier reaches the device registry and HMAC-verifies tokens, like the auth suite
# itself, and the source bodies are async; mark the module accordingly.
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.asyncio]

PEER_KINDS = ("federation", "fleet-service", "paired-agent")
ENROLLED_CONSOLE = "enrolled-console"
_OPERATOR_TIER_PEERS = ("fleet-service", "paired-agent")

# fleet-service and agent-device are operator TIER but NOT human operators. ``resolve_task_owner``
# keys the owner partition on ``trust_tier == "federation"`` alone, so both land in the human
# operator's ``OPERATOR_OWNER`` ("") partition. A peer in that partition sees the operator's
# private steer task in its context listing / owner-scoped parked read, and (#5380, ADR 0131
# P1b.2) the guarded private resume can re-point onto its parked task — the isolation keys on
# trust tier, not ``is_human_operator``. Named, not fixed (LEAK RULE). federation (its own
# partition) and the enrolled console (the human operator itself) are isolated and pass.
_LEAK = (
    "PROOF FAILURE: an operator-tier but non-human peer (fleet-service / agent-device) shares the "
    "human operator's OPERATOR_OWNER ('') partition, because resolve_task_owner keys the partition "
    "on trust_tier=='federation' alone. So an owner-scoped privacy boundary cannot tell such a peer "
    "from the operator: the peer sees the operator's private steer task and a guarded private resume "
    "can land on the peer's own parked task. Isolation keys on trust tier, not is_human_operator."
)


def _kinds(*, leak: bool):
    """The four caller-kind params. ``leak`` xfails the two operator-tier peer kinds whose
    owner-partition sharing defeats a scenario's isolation boundary."""
    params = []
    for kind in PEER_KINDS:
        bad = leak and kind in _OPERATOR_TIER_PEERS
        marks = [pytest.mark.xfail(strict=True, reason=_LEAK)] if bad else []
        params.append(pytest.param(kind, marks=marks, id=kind))
    params.append(pytest.param(ENROLLED_CONSOLE, id=ENROLLED_CONSOLE))
    return params


def _authed_state(token: str) -> dict:
    """The call-context state the REAL ``A2AAuthMiddleware`` + ``tier_context_builder`` stamp for
    ``token`` — the verified principal kind and trust tier, never a hand-set value."""
    captured: dict = {}

    def inspect(request):
        captured["state"] = dict(tier_context_builder().build(request).state)
        return JSONResponse({})

    app = Starlette(routes=[Route("/a2a", inspect, methods=["GET", "POST"])])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        response = client.post("/a2a", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, (token, response.status_code)
    return captured["state"]


@pytest.fixture
def principals(delegation_env, tmp_path, monkeypatch):
    """Factory for real-auth participants: three peer kinds straight through ``A2AAuthMiddleware``,
    the human operator via a verified fleet-proxy delegation. ``delegation_env`` (tests/conftest.py)
    is the deterministic hub keypair the fleet-proxy suite uses. No hand-set tier anywhere."""
    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "caller-devices.json")
    for name in ("A2A_AUTH_TOKEN", "A2A_FEDERATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def _reset():
        auth._BEARER[0] = auth._FEDERATION[0] = auth._FLEET[0] = None
        auth._API_KEY[0] = ""
        auth._ALLOWED_ORIGINS[0] = auth._MEMBER_PUBLIC[0] = None
        auth._PLUGIN_FEDERATION[:] = []

    _reset()
    # One closed instance offering every tier at once, so one classifier run picks each kind.
    auth.configure(bearer_token="shared", federation_token="fed", fleet_token="fleet",
                   api_key="", allowed_origins_raw="")

    def human_operator() -> ServerCallContext:
        """The enrolled console's human operator: the hub classifies the shared bearer as a human
        kind, mints a one-member delegation, and the member redeems that VERIFIED delegation to a
        ``shared-operator-bearer`` principal (ADR 0131 P1a.2). Never a hand-set operator tier."""
        from graph.fleet.proxy import mint_member_delegation

        hub = _authed_state("shared")
        assert auth.is_human_operator(hub["principal_kind"])
        hub_req = SimpleNamespace(state=SimpleNamespace(principal_kind=hub["principal_kind"]))
        token = mint_member_delegation("member", hub_req, method="SendMessage",
                                       context="delegation-ctx", operation="delegation-op", task_id="")
        assert token, "a human operator must earn a fleet-proxy delegation"
        grant = auth.resolve_human_operator(
            SimpleNamespace(headers={delegation_env.DELEGATION_HEADER: token}),
            audience="member", method="SendMessage", context="delegation-ctx",
            operation="delegation-op", task_id="", idempotent=True)
        assert grant is not None and auth.is_human_operator(grant.principal_kind)
        return ServerCallContext(state={"principal_kind": grant.principal_kind})

    def peer(kind: str, *, requester_principal: str | None = None) -> ServerCallContext:
        """A verified NON-human peer of ``kind``. ``enrolled-console`` is the human operator (never
        a denied peer), so its runs use a representative federation peer while the human operator
        carries the private content."""
        if kind == "fleet-service":
            state = _authed_state("fleet")
        elif kind == "paired-agent":
            code, _ = devices.start_pairing(kind="agent")
            _, token = devices.claim_pairing(code, "proof-agent")
            state = _authed_state(token)
        else:  # federation, and the enrolled-console stand-in peer
            state = _authed_state("fed")
        if requester_principal is not None:
            state["requester_principal"] = requester_principal
        return ServerCallContext(state=state)

    yield SimpleNamespace(human_operator=human_operator, peer=peer)
    _reset()


def _route(monkeypatch, principals, kind: str) -> None:
    """Route the source test's inline ``ServerCallContext(state=...)`` constructions to real-auth
    contexts: the ``shared-operator-bearer`` operator → the delegated human; every peer → ``kind``.
    Patches both the class's home module and the source module's bound name."""
    operator_ctx = principals.human_operator()

    def fake(state=None, **kwargs):
        s = dict(state or {})
        if s.get("principal_kind") == "shared-operator-bearer":
            return operator_ctx
        return principals.peer(kind, requester_principal=s.get("requester_principal"))

    monkeypatch.setattr(_scc, "ServerCallContext", fake)
    monkeypatch.setattr(src, "ServerCallContext", fake)


# ── Re-run each scenario for every caller kind (bodies imported, assertions unchanged) ────


@pytest.mark.parametrize("kind", _kinds(leak=False))
@pytest.mark.parametrize("parked", [False, True])
async def test_output_gate_cannot_hide_private_input_history(
    tmp_path, delegation_env, monkeypatch, principals, parked, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_phase0_output_gate_cannot_hide_private_input_history(tmp_path, delegation_env, parked)


@pytest.mark.parametrize("kind", _kinds(leak=True))
async def test_private_steer_into_a_peer_thread(tmp_path, delegation_env, monkeypatch, principals, kind):
    _route(monkeypatch, principals, kind)
    await src.test_a_private_steer_into_a_peer_thread_hides_the_input_and_keeps_the_thread_the_peers(
        tmp_path, delegation_env)


@pytest.mark.parametrize("kind", _kinds(leak=True))
async def test_private_resume_never_lands_on_a_peer_owned_parked_task(
    tmp_path, delegation_env, monkeypatch, principals, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_a_private_resume_never_lands_on_a_peer_owned_parked_task(tmp_path, delegation_env)


@pytest.mark.parametrize("kind", _kinds(leak=False))
async def test_preexisting_handles(tmp_path, monkeypatch, principals, kind):
    _route(monkeypatch, principals, kind)
    await src.test_phase0_preexisting_handles(tmp_path)


@pytest.mark.parametrize("kind", _kinds(leak=False))
async def test_private_final_envelope_keeps_selected_files_across_resume(
    tmp_path, monkeypatch, principals, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_private_final_envelope_keeps_selected_files_across_resume(tmp_path, monkeypatch)


_FAULTS = [
    ("completion", TaskState.TASK_STATE_COMPLETED, True, "completed"),
    ("tool_error", TaskState.TASK_STATE_COMPLETED, True, "completed"),
    ("model_error", TaskState.TASK_STATE_FAILED, False, "failed"),
    ("crash", TaskState.TASK_STATE_FAILED, False, "failed"),
    ("cancel", TaskState.TASK_STATE_CANCELED, False, "canceled"),
    ("timeout", TaskState.TASK_STATE_FAILED, False, "failed"),
]


@pytest.mark.parametrize("kind", _kinds(leak=False))
@pytest.mark.parametrize("fault,terminal,delivers,outcome_state", _FAULTS)
async def test_private_segment_exit_never_leaks_and_closes_the_delivery(
    tmp_path, monkeypatch, principals, fault, terminal, delivers, outcome_state, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_a_private_segment_exit_never_leaks_and_closes_the_delivery(
        tmp_path, fault, terminal, delivers, outcome_state)
