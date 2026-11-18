"""Shared single-member lifecycle ops used by console and agent adapters."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from graph.fleet import proxy, supervisor
from ops import fleet, registry


@pytest.mark.asyncio
async def test_member_lifecycle_results_and_domain_errors(monkeypatch):
    running = {"id": "member", "running": True}
    survived = {"name": "member", "stopped": False, "reason": "still alive"}
    start = Mock(return_value=running)
    stop = Mock(return_value=survived)
    monkeypatch.setattr(supervisor, "start", start)
    monkeypatch.setattr(supervisor, "stop", stop)
    # Unconditional stops need no reachable member or readiness request.
    resolve = Mock(side_effect=AssertionError("unexpected readiness check"))
    monkeypatch.setattr(proxy, "_resolve_slug", resolve)
    assert await fleet.start("member") == running
    assert await fleet.stop("member") == survived
    error = supervisor.FleetError("not running")
    for operation, core in ((fleet.start, start), (fleet.stop, stop)):
        core.side_effect = error
        with pytest.raises(supervisor.FleetError) as caught:
            await operation("member")
        assert caught.value is error
    assert registry()["fleet.start"].mutates is True
    assert registry()["fleet.stop"].mutates is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "host", "remote"])
async def test_idle_restart_rejects_nonlocal_or_missing_target(monkeypatch, kind):
    monkeypatch.setattr(proxy, "_resolve_slug", lambda name: (kind, "http://member", None) if kind else None)
    client = Mock()
    monkeypatch.setattr(proxy, "_get_client", client)
    stop = Mock()
    monkeypatch.setattr(supervisor, "stop", stop)
    with pytest.raises(fleet.RestartNotReady, match="The agent is not reachable; no restart was attempted."):
        await fleet.stop("member", when_idle=True)
    client.assert_not_called()
    stop.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,content,message",
    [
        (200, b'{"idle": 1}', "The agent became busy"),
        (200, b'{"idle": "true"}', "The agent became busy"),
        (200, b'{}', "The agent became busy"),
        (503, b'{"idle": true}', "Could not check whether the agent is idle"),
        (200, b'not json', "Could not check whether the agent is idle"),
    ],
)
async def test_idle_restart_requires_successful_explicit_readiness(monkeypatch, status, content, message):
    monkeypatch.setattr(proxy, "_resolve_slug", lambda name: ("local", "http://member", None))
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "service")
    response = httpx.Response(status, content=content, request=httpx.Request("GET", "http://member"))
    monkeypatch.setattr(proxy, "_get_client", lambda: Mock(get=AsyncMock(return_value=response)))
    stop = Mock()
    monkeypatch.setattr(supervisor, "stop", stop)
    with pytest.raises(fleet.RestartNotReady, match=message):
        await fleet.stop("member", when_idle=True)
    stop.assert_not_called()
