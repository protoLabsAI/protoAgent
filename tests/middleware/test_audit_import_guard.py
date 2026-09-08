"""AuditMiddleware must not fail a tool call when observability can't be imported (#3366).

The middleware resolved ``observability.audit`` with an unguarded function-local
import on every tool call, so a momentarily unresolvable module — the #2298
live-checkout hazard, where a lazy import lands mid-edit or mid-branch-switch —
raised straight out of the middleware and failed the whole turn. Observability is
not load-bearing; these tests pin that the tool call survives it.

The failure is reproduced the way it actually presents: ``observability`` itself
still resolves and only the ``audit`` submodule is missing, which is why the
production error read ``No module named 'observability.audit'``.
"""

import builtins
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from graph.middleware import audit as audit_mod
from graph.middleware.audit import AuditMiddleware


@contextlib.contextmanager
def _audit_unimportable():
    """Make ``from observability.audit import audit_logger`` raise, and nothing else."""
    real_import = builtins.__import__

    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "observability.audit":
            raise ModuleNotFoundError("No module named 'observability.audit'")
        return real_import(name, globals, locals, fromlist, level)

    with patch.object(builtins, "__import__", _blocked):
        yield


@pytest.fixture(autouse=True)
def _reset_warn_once():
    """The warn-once flag is module state — reset it so tests don't order-couple."""
    audit_mod._OBS_UNAVAILABLE_LOGGED = False
    yield
    audit_mod._OBS_UNAVAILABLE_LOGGED = False


def _request(name="fetch_data", args=None):
    req = MagicMock()
    req.tool_call = {"name": name, "args": args if args is not None else {"url": "https://example.com"}}
    return req


def _tool_message(content="the tool result"):
    msg = MagicMock()
    msg.content = content
    return msg


def test_sync_tool_call_survives_unimportable_audit():
    """The sync path returns the tool's result instead of raising ModuleNotFoundError."""
    result = _tool_message()
    with _audit_unimportable():
        out = AuditMiddleware()._handle_tool_call(_request(), lambda r: result)
    assert out is result


async def test_async_tool_call_survives_unimportable_audit():
    """The async path — the one that failed in production on a2a turns."""
    result = _tool_message()

    async def _handler(_req):
        return result

    with _audit_unimportable():
        out = await AuditMiddleware()._ahandle_tool_call(_request(), _handler)
    assert out is result


def test_failing_tool_still_raises_its_own_error():
    """The except path audits then re-raises. With audit degraded it must re-raise the
    TOOL's exception — swapping it for a ModuleNotFoundError would hide the real fault."""

    def _handler(_req):
        raise ValueError("the tool itself broke")

    with _audit_unimportable(), pytest.raises(ValueError, match="the tool itself broke"):
        AuditMiddleware()._handle_tool_call(_request(), _handler)


def test_degradation_is_logged_once_not_per_call(caplog):
    """A retry storm must not turn the log into a traceback firehose."""
    result = _tool_message()
    with _audit_unimportable(), caplog.at_level("ERROR", logger="graph.middleware.audit"):
        for _ in range(5):
            AuditMiddleware()._handle_tool_call(_request(), lambda r: result)

    hits = [r for r in caplog.records if "observability unavailable" in r.getMessage()]
    assert len(hits) == 1


def test_recovers_once_the_module_resolves_again():
    """Nothing is cached: a transient failure must not disable auditing for the
    life of the process."""
    result = _tool_message()
    with _audit_unimportable():
        AuditMiddleware()._handle_tool_call(_request(), lambda r: result)

    audit_logger, tracing, metrics = audit_mod._observability()
    assert audit_logger is not audit_mod._NoopAudit
    assert tracing is not audit_mod._NoopTracing
    assert metrics is not audit_mod._NoopMetrics
