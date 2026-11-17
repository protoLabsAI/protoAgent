"""P1 proof K1: the A2A privacy proofs hold for every KIND of caller, not one identity.

The proof matrix in ``docs/design/conversations-in-work-proposal.md`` requires each identity-sensitive
scenario to run for federation, fleet-service, paired-agent and enrolled-console callers, with no
operator-tier shortcut. The scenarios in ``tests/test_a2a_hitl_parked_routing.py`` each prove privacy
with ONE hand-set caller. This file RE-RUNS six of them without copying their bodies: it imports each
source test and routes the inline ``ServerCallContext(state=...)`` builds to principals made through
the REAL auth path (``A2AAuthMiddleware``, as in ``test_a2a_auth::test_verified_principal_in_http_call_
context``), never a hand-set tier. The peer is authenticated as each kind; the operator is a human
reached via a verified fleet-proxy delegation (ADR 0131 P1a.2) that also runs the source's ``CALL``
sends. Original assertions are kept verbatim. A real leak is a strict ``xfail`` named "PROOF FAILURE"
(PR body ``## Proof failures``), scoped ``raises=AssertionError`` so only the source test's bare-assert
check — not this harness's ``_require``/``_HarnessError`` setup guards — satisfies it.
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

pytestmark = [pytest.mark.platform_sensitive, pytest.mark.asyncio]  # HMAC token verify + async bodies

FEDERATION = "federation"
ENROLLED_CONSOLE = "enrolled-console"
ALL_KINDS = (FEDERATION, "fleet-service", "paired-agent", ENROLLED_CONSOLE)
# All operator TIER share OPERATOR_OWNER ("") — resolve_task_owner keys on trust_tier; only federation isolates.
_OPERATOR_TIER_PEERS = ("fleet-service", "paired-agent", ENROLLED_CONSOLE)

# Real product defects, named not fixed (LEAK RULE). Full detail in the PR body.
_STEER_LEAK = (
    "PROOF FAILURE: an operator-tier peer shares the operator's OPERATOR_OWNER ('') partition "
    "(resolve_task_owner keys on trust_tier, not identity), so the operator's private steer — a PUBLIC "
    "operator-owned task — shows up in the peer's own on_list_tasks/on_get_task. Only federation isolates."
)
_RESUME_LEAK = (
    "PROOF FAILURE: an operator-tier peer shares the operator's OPERATOR_OWNER ('') partition "
    "(resolve_task_owner keys on trust_tier, not identity), so the peer's parked task appears in the "
    "operator's OWNER-SCOPED parked read and the 'operator owns no parked task' assert fails there."
)
_PRIVATE_PAYLOAD_LEAK = (
    "PROOF FAILURE: an enrolled-console (paired human 'device') peer is is_human_operator AND shares the "
    "operator's OPERATOR_OWNER ('') partition, so a server/console read returns the operator's "
    "private_payload: the gate keys on the credential kind plus the trust-tier owner, not identity."
)


class _HarnessError(RuntimeError):
    """A failure in THIS file's real-auth harness setup — NOT a privacy leak. ``_require`` raises it
    (not ``AssertionError``) so a broken setup can't satisfy a PROOF FAILURE ``xfail``."""


def _require(cond, msg):
    """Validate a harness precondition — raises ``_HarnessError`` (not ``AssertionError``), so a
    setup failure never counts as a privacy PROOF FAILURE."""
    if not cond:
        raise _HarnessError(msg)


def _kinds(*, leak_kinds=(), reason=None):
    """The four caller-kind params; ``leak_kinds`` get a strict ``xfail`` scoped to ``AssertionError``
    with ``reason`` (so only the real privacy assertion, never a setup error, satisfies them)."""
    params = []
    for kind in ALL_KINDS:
        if kind in leak_kinds:
            _require(reason is not None, "a leaking kind needs a PROOF FAILURE reason")
            marks = [pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)]
        else:
            marks = []
        params.append(pytest.param(kind, marks=marks, id=kind))
    return params


def _authed_state(token: str) -> dict:
    """The state the REAL ``A2AAuthMiddleware`` + ``tier_context_builder`` stamp for ``token``."""
    captured: dict = {}

    def inspect(request):
        captured["state"] = dict(tier_context_builder().build(request).state)
        return JSONResponse({})

    app = Starlette(routes=[Route("/a2a", inspect, methods=["GET", "POST"])])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        response = client.post("/a2a", headers={"Authorization": f"Bearer {token}"})
    _require(response.status_code == 200, (token, response.status_code))
    return captured["state"]


@pytest.fixture
def principals(delegation_env, tmp_path, monkeypatch):
    """Factory for real-auth participants: the four peer kinds, and the delegated human operator."""
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
        """The private-content operator: a human reached by redeeming a verified fleet-proxy delegation
        to a ``shared-operator-bearer`` (ADR 0131 P1a.2); the hub classifies the shared bearer first."""
        from graph.fleet.proxy import mint_member_delegation

        hub = _authed_state("shared")
        _require(auth.is_human_operator(hub["principal_kind"]),
                 ("the shared bearer must classify as a human operator", hub.get("principal_kind")))
        hub_req = SimpleNamespace(state=SimpleNamespace(principal_kind=hub["principal_kind"]))
        token = mint_member_delegation("member", hub_req, method="SendMessage",
                                       context="delegation-ctx", operation="delegation-op", task_id="")
        _require(token, "a human operator must earn a fleet-proxy delegation")
        grant = auth.resolve_human_operator(
            SimpleNamespace(headers={delegation_env.DELEGATION_HEADER: token}),
            audience="member", method="SendMessage", context="delegation-ctx",
            operation="delegation-op", task_id="", idempotent=True)
        _require(grant is not None and auth.is_human_operator(grant.principal_kind),
                 ("the member must redeem the delegation to a human operator", grant))
        return ServerCallContext(state={"principal_kind": grant.principal_kind})

    def peer(kind: str, *, requester_principal: str | None = None) -> ServerCallContext:
        """A verified peer of ``kind`` through the real middleware (``enrolled-console`` = paired human)."""
        if kind == "fleet-service":
            state = _authed_state("fleet")
        elif kind == "paired-agent":
            code, _ = devices.start_pairing(kind="agent")
            _, token = devices.claim_pairing(code, "proof-agent")
            state = _authed_state(token)
        elif kind == ENROLLED_CONSOLE:
            code, _ = devices.start_pairing(kind="device")
            _, token = devices.claim_pairing(code, "proof-console")
            state = _authed_state(token)
            # the console IS a human operator (setup invariant, not a privacy assertion)
            _require(auth.is_human_operator(state["principal_kind"]),
                     ("an enrolled console must classify as a human operator", state.get("principal_kind")))
        else:  # federation
            state = _authed_state("fed")
        if requester_principal is not None:
            state["requester_principal"] = requester_principal
        return ServerCallContext(state=state)

    yield SimpleNamespace(human_operator=human_operator, peer=peer)
    _reset()


# The genuine class, captured before any patch, so unmarked/internal constructions pass through.
_REAL_SCC = ServerCallContext


def _route(monkeypatch, principals, kind: str) -> None:
    """Route the source's inline ``ServerCallContext(state=...)`` builds: the operator and its module-
    level ``CALL`` → the delegated human; peer-shaped state → ``kind``; anything else → the real class."""
    operator_ctx = principals.human_operator()

    def fake(state=None, **kwargs):
        s = dict(state or {})
        pk, tt = s.get("principal_kind"), s.get("trust_tier")
        if pk == "shared-operator-bearer":
            return operator_ctx
        if pk in ("federation", "paired-agent") or tt == "federation" or "requester_principal" in s:
            return principals.peer(kind, requester_principal=s.get("requester_principal"))
        return _REAL_SCC(state=state, **kwargs)

    monkeypatch.setattr(_scc, "ServerCallContext", fake)
    monkeypatch.setattr(src, "ServerCallContext", fake)
    monkeypatch.setattr(src, "CALL", operator_ctx)


# ── Re-run each scenario for every caller kind (bodies imported, assertions unchanged) ────


@pytest.mark.parametrize("kind", _kinds(leak_kinds=(ENROLLED_CONSOLE,), reason=_PRIVATE_PAYLOAD_LEAK))
@pytest.mark.parametrize("parked", [False, True])
async def test_output_gate_cannot_hide_private_input_history(
    tmp_path, delegation_env, monkeypatch, principals, parked, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_phase0_output_gate_cannot_hide_private_input_history(tmp_path, delegation_env, parked)


@pytest.mark.parametrize("kind", _kinds(leak_kinds=_OPERATOR_TIER_PEERS, reason=_STEER_LEAK))
async def test_private_steer_into_a_peer_thread(tmp_path, delegation_env, monkeypatch, principals, kind):
    _route(monkeypatch, principals, kind)
    await src.test_a_private_steer_into_a_peer_thread_hides_the_input_and_keeps_the_thread_the_peers(
        tmp_path, delegation_env)


@pytest.mark.parametrize("kind", _kinds(leak_kinds=_OPERATOR_TIER_PEERS, reason=_RESUME_LEAK))
async def test_private_resume_never_lands_on_a_peer_owned_parked_task(
    tmp_path, delegation_env, monkeypatch, principals, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_a_private_resume_never_lands_on_a_peer_owned_parked_task(tmp_path, delegation_env)


@pytest.mark.parametrize("kind", _kinds(leak_kinds=(ENROLLED_CONSOLE,), reason=_PRIVATE_PAYLOAD_LEAK))
async def test_preexisting_handles(tmp_path, monkeypatch, principals, kind):
    _route(monkeypatch, principals, kind)
    await src.test_phase0_preexisting_handles(tmp_path)


@pytest.mark.parametrize("kind", _kinds())
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


@pytest.mark.parametrize("kind", _kinds(leak_kinds=(ENROLLED_CONSOLE,), reason=_PRIVATE_PAYLOAD_LEAK))
@pytest.mark.parametrize("fault,terminal,delivers,outcome_state", _FAULTS)
async def test_private_segment_exit_never_leaks_and_closes_the_delivery(
    tmp_path, monkeypatch, principals, fault, terminal, delivers, outcome_state, kind
):
    _route(monkeypatch, principals, kind)
    await src.test_a_private_segment_exit_never_leaks_and_closes_the_delivery(
        tmp_path, fault, terminal, delivers, outcome_state)
