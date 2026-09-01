"""Tests for the Langfuse tracing module.

The hot path here is ``trace_session`` — an async context manager that
makes a Langfuse observation the active parent for its body. These tests
verify the wiring survives a re-arrangement without regression:

- When Langfuse is disabled, every helper is a silent no-op (never raises,
  never holds state).
- When enabled, ``trace_session`` calls ``start_as_current_observation``
  AND enters the returned context manager — the previous API created the
  span but never entered its scope, so children didn't nest.
- ``current_trace_id()`` reads the contextvar set on entry and clears on
  exit; nested sessions restore the outer trace id.
- ``trace_tool_call`` stamps the current trace_id into its metadata so
  audit-log cross-ref works even if Langfuse later reshapes the span tree.

The tests don't require the real langfuse package — a minimal fake client
with the three methods we touch covers the contract.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def _reload_tracing():
    """Fresh module import of the real tracing.py so each test starts
    from init=disabled, even if a sibling test file inserted a stub
    into sys.modules first (test_exception_logging.py does this)."""
    import importlib.util
    from pathlib import Path

    if "tracing" in sys.modules:
        del sys.modules["observability.tracing"]
    real_path = Path(__file__).parents[1] / "observability" / "tracing.py"
    spec = importlib.util.spec_from_file_location("observability.tracing", real_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["observability.tracing"] = module
    spec.loader.exec_module(module)
    return module


def _enable_with_fake_client(tracing):
    """Inject a fake Langfuse client and flip _enabled. Returns the fake."""
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "trace-abc"
    # start_as_current_observation returns a context manager
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    # start_observation returns an observation with .end()
    child = MagicMock()
    fake.start_observation.return_value = child
    tracing._langfuse = fake
    tracing._enabled = True
    return fake, span, child


# ── Disabled (no Langfuse) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_trace_session_is_noop_context_manager():
    tracing = _reload_tracing()
    assert tracing.is_enabled() is False

    async with tracing.trace_session("s-1", name="x") as span:
        assert span is None
        assert tracing.current_trace_id() == ""
        # session_id is set even when Langfuse is disabled
        assert tracing.current_session_id() == "s-1"

    # Calls outside a session return default ""
    assert tracing.current_trace_id() == ""
    assert tracing.current_session_id() == ""


def test_disabled_trace_tool_call_returns_none():
    tracing = _reload_tracing()
    assert tracing.trace_tool_call("t", {}, "ok", 10, True) is None


def test_disabled_score_current_trace_is_silent():
    tracing = _reload_tracing()
    tracing.score_current_trace("verdict", 1.0)  # must not raise


# ── Enabled (fake Langfuse) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_session_enters_context_and_exposes_trace_id():
    """Regression: the previous API called start_as_current_observation
    without `with`, so the span was created but its scope was never active.
    Children never nested. Lock that trace_session enters the CM."""
    tracing = _reload_tracing()
    fake, span, _child = _enable_with_fake_client(tracing)

    captured_trace_id = None
    captured_session_id = None

    async with tracing.trace_session("s-abc", name="a2a-stream"):
        captured_trace_id = tracing.current_trace_id()
        captured_session_id = tracing.current_session_id()

    # start_as_current_observation was called with the right name + metadata
    fake.start_as_current_observation.assert_called_once()
    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "a2a-stream"
    assert kwargs["metadata"]["session_id"] == "s-abc"
    assert "protoagent" in kwargs["metadata"]["tags"]

    # AND the returned CM was actually entered (the bug fix)
    cm = fake.start_as_current_observation.return_value
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()

    # current_trace_id reflected the span inside the scope, clears outside
    assert captured_trace_id == "trace-abc"
    assert tracing.current_trace_id() == ""
    # session_id is set by trace_session and cleared on exit
    assert captured_session_id == "s-abc"
    assert tracing.current_session_id() == ""


@pytest.mark.asyncio
async def test_disabled_trace_session_swallows_cross_context_reset_error():
    """Regression: with Langfuse disabled (the local default), an SSE client
    disconnecting mid-stream tears down the async generator in a different
    context, so ``_session_id_ctx.reset(token)`` raises ``ValueError: ... was
    created in a different Context``. trace_session's finally must swallow it
    instead of letting it escape as an unretrieved task exception."""
    tracing = _reload_tracing()
    assert tracing.is_enabled() is False

    real = tracing._session_id_ctx

    class _ResetBoom:
        def set(self, value):
            return real.set(value)

        def get(self):
            return real.get()

        def reset(self, _token):
            raise ValueError("was created in a different Context")

    tracing._session_id_ctx = _ResetBoom()
    try:
        # Must not raise despite reset() blowing up in the finally block.
        async with tracing.trace_session("s-boom", name="x") as span:
            assert span is None
    finally:
        tracing._session_id_ctx = real


@pytest.mark.asyncio
async def test_trace_session_exception_is_swallowed_so_agent_keeps_running():
    """If Langfuse itself raises, the agent must not crash. trace_session
    yields None and the caller proceeds unscoped."""
    tracing = _reload_tracing()
    fake = MagicMock()
    fake.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    tracing._langfuse = fake
    tracing._enabled = True

    async with tracing.trace_session("s-err") as span:
        assert span is None


@pytest.mark.asyncio
async def test_trace_tool_call_stamps_current_trace_id_into_metadata():
    """Audit cross-ref contract: the tool observation carries the
    current trace_id in its metadata so an audit-log line (which also
    records trace_id) can be matched to the exact Langfuse trace."""
    tracing = _reload_tracing()
    fake, _span, child = _enable_with_fake_client(tracing)

    async with tracing.trace_session("s-1", name="parent"):
        tracing.trace_tool_call(
            tool_name="board_monitor",
            args={"action": "sitrep"},
            result="ok",
            duration_ms=42,
            success=True,
            session_id="s-1",
        )

    fake.start_observation.assert_called_once()
    kwargs = fake.start_observation.call_args.kwargs
    assert kwargs["name"] == "tool:board_monitor"
    assert kwargs["metadata"]["trace_id"] == "trace-abc"
    assert kwargs["metadata"]["duration_ms"] == 42
    assert kwargs["level"] == "DEFAULT"
    child.end.assert_called_once()


def test_trace_tool_call_on_failure_marks_error_level():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    tracing.trace_tool_call(
        tool_name="file_bug",
        args={},
        result="boom",
        duration_ms=10,
        success=False,
    )
    kwargs = fake.start_observation.call_args.kwargs
    assert kwargs["level"] == "ERROR"


def test_score_current_trace_delegates_to_client():
    tracing = _reload_tracing()
    fake, _s, _c = _enable_with_fake_client(tracing)
    tracing.score_current_trace("verdict", 1.0, comment="PASS")
    fake.score_current_trace.assert_called_once_with(
        name="verdict",
        value=1.0,
        comment="PASS",
    )


# ── Fleet tracing: caller trace join (a2a.trace → trace_context) ─────────────

_TID = "a" * 32  # valid W3C trace id (32 hex)
_SID = "b" * 16  # valid W3C span id (16 hex)


@pytest.mark.asyncio
async def test_trace_session_joins_caller_trace_context():
    """When metadata carries caller_trace_id/caller_span_id (the a2a.trace ids
    an upstream agent sent), the session span JOINS that trace via Langfuse's
    trace_context — and current_trace_id() reports the JOINED id so audit
    records + downstream propagation carry the fleet trace."""
    tracing = _reload_tracing()
    fake, span, _child = _enable_with_fake_client(tracing)
    span.trace_id = _TID  # the SDK reports the joined trace id on the span

    async with tracing.trace_session(
        "s-1", name="a2a-stream", metadata={"caller_trace_id": _TID, "caller_span_id": _SID}
    ):
        assert tracing.current_trace_id() == _TID

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": _TID, "parent_span_id": _SID}
    # The metadata stamping is KEPT (operators cross-reference by it too)
    assert kwargs["metadata"]["caller_trace_id"] == _TID
    assert kwargs["metadata"]["caller_span_id"] == _SID


@pytest.mark.asyncio
async def test_trace_session_join_without_span_id_still_joins_trace():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    async with tracing.trace_session("s-1", metadata={"caller_trace_id": _TID}):
        pass

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": _TID}


@pytest.mark.asyncio
async def test_trace_session_malformed_caller_ids_fall_back_to_fresh_trace():
    """Malformed caller ids must never crash a turn or feed the SDK a bogus
    W3C context — the session degrades to a fresh trace."""
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    async with tracing.trace_session(
        "s-1", metadata={"caller_trace_id": "not-a-trace-id", "caller_span_id": "zz"}
    ) as span:
        assert span is not None  # the session still traces — freshly
        assert tracing.current_trace_id() == "trace-abc"

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] is None


@pytest.mark.asyncio
async def test_trace_session_join_with_malformed_span_id_keeps_trace_id_only():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    async with tracing.trace_session("s-1", metadata={"caller_trace_id": _TID, "caller_span_id": "junk"}):
        pass

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": _TID}


# ── current_trace_context (outbound propagation helper) ──────────────────────


def test_disabled_current_trace_context_is_none():
    tracing = _reload_tracing()
    assert tracing.current_trace_context() is None


def test_current_trace_context_shape_from_sdk():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.return_value = _TID
    fake.get_current_observation_id.return_value = _SID
    assert tracing.current_trace_context() == {"trace_id": _TID, "span_id": _SID}


def test_current_trace_context_without_current_span_has_trace_id_only():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.return_value = _TID
    fake.get_current_observation_id.return_value = None
    assert tracing.current_trace_context() == {"trace_id": _TID}


def test_current_trace_context_none_when_no_active_trace():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.return_value = None
    fake.get_current_observation_id.return_value = None
    assert tracing.current_trace_context() is None


def test_current_trace_context_falls_back_to_contextvar_when_sdk_errors():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    fake.get_current_trace_id.side_effect = RuntimeError("otel misery")
    fake.get_current_observation_id.side_effect = RuntimeError("otel misery")
    token = tracing._trace_id_ctx.set(_TID)
    try:
        assert tracing.current_trace_context() == {"trace_id": _TID}
    finally:
        tracing._trace_id_ctx.reset(token)


# ── trace_span (boundary spans, e.g. subagent:<type>) ────────────────────────


def test_disabled_trace_span_is_noop():
    tracing = _reload_tracing()
    with tracing.trace_span("subagent:worker") as span:
        assert span is None


def test_trace_span_opens_and_closes_child_observation():
    tracing = _reload_tracing()
    fake, span, _child = _enable_with_fake_client(tracing)

    with tracing.trace_span("subagent:worker", metadata={"description": "d"}, as_type="agent") as s:
        assert s is span

    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "subagent:worker"
    assert kwargs["as_type"] == "agent"
    assert kwargs["metadata"] == {"description": "d"}
    cm = fake.start_as_current_observation.return_value
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()


def test_trace_span_body_exception_propagates_but_span_closes():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)

    with pytest.raises(RuntimeError):
        with tracing.trace_span("subagent:worker"):
            raise RuntimeError("subagent exploded")

    cm = fake.start_as_current_observation.return_value
    cm.__exit__.assert_called_once()


def test_trace_span_sdk_error_yields_none_and_body_runs():
    tracing = _reload_tracing()
    fake = MagicMock()
    fake.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    tracing._langfuse = fake
    tracing._enabled = True

    ran = False
    with tracing.trace_span("subagent:worker") as span:
        assert span is None
        ran = True
    assert ran


# ── init: env-or-config credentials (#3017) ───────────────────────────────────
#
# Before #3017 ``init`` read LANGFUSE_{PUBLIC,SECRET}_KEY from os.environ and nowhere
# else, so tracing could not be enabled on a desktop-launched fleet member (nothing in
# that launch path sets those vars). These lock the precedence the fix chose: the
# environment still WINS so container deploys are untouched, and config is the fallback
# beneath it — asserted on the resulting client/state, never on "a helper was called".


class _FakeLangfuse:
    """Stands in for the real SDK client so init() can be driven without langfuse
    installed. Records the kwargs it was constructed with — those ARE the outcome
    under test (which credentials and host actually reached the client)."""

    def __init__(self, public_key="", secret_key="", host=""):
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host


def _install_fake_langfuse(monkeypatch):
    """Make ``from langfuse import Langfuse`` resolve to _FakeLangfuse."""
    import types

    mod = types.ModuleType("langfuse")
    mod.Langfuse = _FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", mod)


class _Cfg:
    """The tracing-relevant slice of LangGraphConfig. ``resolve_credentials`` reads
    it via getattr (observability/ sits below graph/ in the import layering), so a
    plain object is the honest stand-in."""

    def __init__(self, enabled=False, host="", public_key="", secret_key=""):
        self.tracing_enabled = enabled
        self.tracing_host = host
        self.tracing_public_key = public_key
        self.tracing_secret_key = secret_key


def _clear_langfuse_env(monkeypatch):
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_init_with_no_env_and_no_config_stays_disabled(monkeypatch, capsys):
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)

    tracing.init()

    assert tracing.is_enabled() is False
    assert tracing._langfuse is None
    assert "Langfuse not configured" in capsys.readouterr().out


def test_init_from_config_only_credentials_enables_tracing(monkeypatch):
    """The #3017 acceptance: a desktop-launched fleet member has no LANGFUSE_* in its
    environment, so the config layer is the only one that can turn tracing on."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)

    tracing.init(config=_Cfg(enabled=True, host="https://cloud.langfuse.com", public_key="pk-cfg", secret_key="sk-cfg"))

    assert tracing.is_enabled() is True
    assert tracing._langfuse.public_key == "pk-cfg"
    assert tracing._langfuse.secret_key == "sk-cfg"
    assert tracing._langfuse.host == "https://cloud.langfuse.com"


def test_env_credentials_beat_config_credentials(monkeypatch):
    """Container deploys rely on the env pair; config must never shadow it."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    monkeypatch.setenv("LANGFUSE_HOST", "https://env.langfuse.example")

    tracing.init(config=_Cfg(enabled=True, host="https://cfg.langfuse.example", public_key="pk-cfg", secret_key="sk-cfg"))

    assert tracing._langfuse.public_key == "pk-env"
    assert tracing._langfuse.secret_key == "sk-env"
    assert tracing._langfuse.host == "https://env.langfuse.example"


def test_env_credentials_win_even_when_the_config_toggle_is_off(monkeypatch):
    """``tracing.enabled`` is a fallback toggle, not a kill switch — an env-configured
    deploy has no config file to flip and must keep tracing."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")

    tracing.init(config=_Cfg(enabled=False))

    assert tracing.is_enabled() is True
    assert tracing._langfuse.public_key == "pk-env"


def test_config_credentials_ignored_while_the_toggle_is_off(monkeypatch):
    """Stored keys with the toggle off = deliberately not tracing, not "connect anyway"."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)

    tracing.init(config=_Cfg(enabled=False, public_key="pk-cfg", secret_key="sk-cfg"))

    assert tracing.is_enabled() is False
    assert tracing._langfuse is None


def test_config_with_half_a_key_pair_stays_disabled_and_says_why(monkeypatch, capsys):
    """Half-configured is the operator mid-setup, not a reason to build a client that
    would 401 on every span — and the boot log has to NAME that, not fold it into the
    same "Langfuse not configured" an untouched instance prints. Someone who flipped
    the toggle and saved would otherwise read that as "my setting didn't take", which
    is the same silence #3017 exists to remove."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)

    tracing.init(config=_Cfg(enabled=True, public_key="pk-cfg"))

    assert tracing.is_enabled() is False
    assert tracing._langfuse is None
    out = capsys.readouterr().out
    assert "tracing.enabled is on" in out and "key pair is incomplete" in out


def test_env_keys_with_no_host_anywhere_keep_the_legacy_default(monkeypatch):
    """The pre-#3017 behavior for an env-only deploy that sets just the pair."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")

    tracing.init()

    assert tracing._langfuse.host == "http://host.docker.internal:3001"


def test_legacy_langfuse_url_env_still_names_the_host(monkeypatch):
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    monkeypatch.setenv("LANGFUSE_URL", "https://legacy.langfuse.example")

    tracing.init()

    assert tracing._langfuse.host == "https://legacy.langfuse.example"


# ── env keys never follow a config host (#3039) ───────────────────────────────
#
# #3017 resolved the host as ``env_host or cfg_host or _DEFAULT_HOST``, independently
# of which layer answered for the KEYS — so config data chose the destination for
# deployment-owned credentials. docker-compose.yml passes ``LANGFUSE_HOST=${LANGFUSE_HOST:-}``,
# which is SET AND EMPTY for an operator who exports only the key pair, so ``cfg_host``
# won that chain and the deployment's keys left the process as a Basic auth header aimed
# wherever ``tracing.host`` said. The block is DIRECTIONAL: env is the more trusted layer,
# so config keys still fall back to ``env_host`` — host-in-env + keys-in-Settings is the
# shape compose and the example YAML tell operators to use, and it is how every fleet member
# runs. These pin both halves on the shapes they actually run in: a populated instance config
# loaded off disk through ``LangGraphConfig.from_yaml`` with the credentials in ``secrets.yaml``
# (where a Settings save puts them), against the compose environment block verbatim — the
# set-and-empty ``LANGFUSE_*`` vars included.


def _compose_env(monkeypatch, *, langfuse_host: str = "", with_keys: bool = True):
    """The environment docker-compose.yml exports, not a minimal one. Every ``LANGFUSE_*``
    var is ``${VAR:-}`` — PRESENT AND EMPTY unless the operator exports one, which is both
    the shape that made #3039 reachable and the shape an operator who keeps the key pair in
    Settings ▸ Tracing actually runs (compose persists /sandbox/config, so secrets.yaml is
    where those two land)."""
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("AGENT_NAME", "protoagent")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-deployment-openai")
    monkeypatch.setenv("A2A_AUTH_TOKEN", "deployment-bearer")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-deployment" if with_keys else "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-deployment" if with_keys else "")
    monkeypatch.setenv("LANGFUSE_HOST", langfuse_host)


def _instance_config(tmp_path, *, tracing_host: str = "", tracing_keys: bool = False):
    """A real ``LangGraphConfig`` read off disk — not the stub — carrying the rest of an
    instance's settings alongside the tracing block, because config reaches an instance
    whole (snapshot import, a fork's committed YAML, any Settings save) and the tracing
    keys live in ``secrets.yaml`` rather than the tracked file (#3017)."""
    import yaml

    from graph.config import LangGraphConfig

    doc = {
        "model": {"provider": "openai", "name": "protolabs/reasoning", "temperature": 0.4},
        "operator": {"allowed_dirs": ["/srv/work"]},
        "plugins": {"enabled": ["artifact", "projectBoard"]},
        "telemetry": {"enabled": True},
        "tracing": {"enabled": True, "host": tracing_host},
    }
    (tmp_path / "langgraph-config.yaml").write_text(yaml.safe_dump(doc))
    if tracing_keys:
        (tmp_path / "secrets.yaml").write_text(
            yaml.safe_dump({"tracing": {"public_key": "pk-lf-settings", "secret_key": "sk-lf-settings"}})
        )
    return LangGraphConfig.from_yaml(tmp_path / "langgraph-config.yaml")


def test_env_keys_with_an_empty_compose_host_never_reach_the_config_host(monkeypatch, tmp_path):
    """The #3039 leak in the deployment shape that made it reachable: compose exports the
    key pair and an EMPTY LANGFUSE_HOST, and the instance's config names a host. The Basic
    auth header these keys build must not be dialed at a destination config chose."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="")
    config = _instance_config(tmp_path, tracing_host="https://collector.attacker.example", tracing_keys=True)

    tracing.init(config=config)

    assert tracing._langfuse.public_key == "pk-lf-deployment"
    assert tracing._langfuse.secret_key == "sk-lf-deployment"
    assert tracing._langfuse.host == "http://host.docker.internal:3001"
    assert "attacker.example" not in tracing._langfuse.host


def test_env_keys_go_to_the_env_host(monkeypatch, tmp_path):
    """The ordinary container deploy: both halves come from the environment, and the
    config naming some other host changes nothing."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="https://langfuse.deployment.example")
    config = _instance_config(tmp_path, tracing_host="https://collector.attacker.example", tracing_keys=True)

    tracing.init(config=config)

    assert tracing._langfuse.public_key == "pk-lf-deployment"
    assert tracing._langfuse.host == "https://langfuse.deployment.example"


def test_config_keys_go_to_the_config_host(monkeypatch, tmp_path):
    """#3017's acceptance, unchanged: a desktop-launched fleet member has no LANGFUSE_* at
    all and configures both halves from Settings ▸ Tracing."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, with_keys=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    config = _instance_config(tmp_path, tracing_host="https://cloud.langfuse.com", tracing_keys=True)

    tracing.init(config=config)

    assert tracing._langfuse.public_key == "pk-lf-settings"
    assert tracing._langfuse.secret_key == "sk-lf-settings"
    assert tracing._langfuse.host == "https://cloud.langfuse.com"


def test_config_keys_still_follow_an_env_host_when_settings_names_none(monkeypatch, tmp_path):
    """The block does NOT mirror, and this is the deployment the mirror would have broken:
    host in the environment, key pair in Settings ▸ Tracing (secrets.yaml), ``tracing.host``
    left blank — exactly what config/langgraph-config.example.yaml tells operators to do,
    and what compose's persisted /sandbox/config gives them. env is the MORE trusted layer
    — only whoever starts the process sets it — so an env host aiming config-owned keys was
    never the #3039 hole. Refusing it would send these traces to _DEFAULT_HOST, which does
    not resolve outside compose, and take tracing silently dark."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="https://langfuse.company.example", with_keys=False)
    config = _instance_config(tmp_path, tracing_host="", tracing_keys=True)

    tracing.init(config=config)

    assert tracing._langfuse.public_key == "pk-lf-settings"
    assert tracing._langfuse.secret_key == "sk-lf-settings"
    assert tracing._langfuse.host == "https://langfuse.company.example"


def test_a_fleet_member_inherits_the_hubs_host_with_its_own_settings_keys(monkeypatch, tmp_path):
    """The #3017 target shape, and the one the mirror direction hit hardest. A member is
    spawned by ``graph/fleet/supervisor.py`` with ``full_env = {**os.environ, **env}`` and
    ``run_exec`` adds only PROTOAGENT_HOME / PROTOAGENT_INSTANCE, so it inherits the hub's
    LANGFUSE_HOST while its keys come from its own per-agent Settings and its own config
    names no host. Its traces belong on the hub's Langfuse, not on host.docker.internal."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="https://langfuse.company.example", with_keys=False)
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "member-home"))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "member-a")
    config = _instance_config(tmp_path, tracing_host="", tracing_keys=True)

    tracing.init(config=config)

    assert tracing._langfuse.host == "https://langfuse.company.example"


def test_a_config_host_beats_a_leftover_env_host_when_config_supplied_the_keys(monkeypatch, tmp_path):
    """The pairing from the operator's side: the host typed into Settings beside those keys
    is the one used, not an unrelated LANGFUSE_HOST in the environment. env_host is the
    FALLBACK for config keys, not an override of them."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="https://langfuse.deployment.example", with_keys=False)
    config = _instance_config(tmp_path, tracing_host="https://cloud.langfuse.com", tracing_keys=True)

    tracing.init(config=config)

    assert tracing._langfuse.host == "https://cloud.langfuse.com"


# ── a losing host is NAMED, never dropped on the floor (#3039) ────────────────
#
# Both discards land on paths that worked before the upgrade, so without a line the
# operator's first signal is a Trace column that stops filling — the silence #3017 exists
# to remove, one layer down. The boot log is where this module already says which layer
# answered, so it is where the ignored value belongs too.


def test_a_discarded_config_host_is_named_on_the_boot_line(monkeypatch, tmp_path, capsys):
    """Env keys + a config host: the config host loses, and boot says so with the value it
    ignored, why, and where the traces went instead."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="")
    config = _instance_config(tmp_path, tracing_host="https://collector.attacker.example", tracing_keys=True)

    tracing.init(config=config)
    out = capsys.readouterr().out

    assert "ignoring tracing.host=https://collector.attacker.example" in out
    assert "LANGFUSE_HOST" in out
    assert "http://host.docker.internal:3001" in out
    assert "[tracing] Langfuse initialized from env -> http://host.docker.internal:3001" in out


def test_a_discarded_env_host_is_named_on_the_boot_line(monkeypatch, tmp_path, capsys):
    """Config keys + hosts on both layers: tracing.host wins and the ignored LANGFUSE_HOST
    is named, so an operator upgrading off #3017 (where env_host won this pair) can see the
    destination moved rather than discover it from an empty Trace column."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="https://langfuse.deployment.example", with_keys=False)
    config = _instance_config(tmp_path, tracing_host="https://cloud.langfuse.com", tracing_keys=True)

    tracing.init(config=config)
    out = capsys.readouterr().out

    assert "ignoring LANGFUSE_HOST=https://langfuse.deployment.example" in out
    assert "tracing.host" in out
    assert "[tracing] Langfuse initialized from config -> https://cloud.langfuse.com" in out


def test_no_ignored_host_line_when_the_two_layers_agree(monkeypatch, tmp_path, capsys):
    """The line is a diagnostic, not noise: naming the same host on both layers discards
    nothing, so nothing is reported."""
    tracing = _reload_tracing()
    _install_fake_langfuse(monkeypatch)
    _compose_env(monkeypatch, langfuse_host="https://langfuse.company.example")
    config = _instance_config(tmp_path, tracing_host="https://langfuse.company.example", tracing_keys=True)

    tracing.init(config=config)
    out = capsys.readouterr().out

    assert tracing._langfuse.host == "https://langfuse.company.example"
    assert "ignoring" not in out


def test_init_is_reentrant_and_never_replaces_a_live_client(monkeypatch):
    """init() runs once at boot today, but the config-aware call site moved (#3017) —
    a second call must not swap the client out from under a running turn."""
    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)
    _install_fake_langfuse(monkeypatch)

    tracing.init(config=_Cfg(enabled=True, public_key="pk-first", secret_key="sk-first"))
    first = tracing._langfuse

    tracing.init(config=_Cfg(enabled=True, public_key="pk-second", secret_key="sk-second"))

    assert tracing._langfuse is first
    assert tracing._langfuse.public_key == "pk-first"


def test_init_survives_a_client_that_raises(monkeypatch):
    """Tracing never fails a boot: an unreachable/rejecting Langfuse degrades to off."""
    import types

    tracing = _reload_tracing()
    _clear_langfuse_env(monkeypatch)

    mod = types.ModuleType("langfuse")

    def _boom(**kwargs):
        raise RuntimeError("langfuse rejected the credentials")

    mod.Langfuse = _boom
    monkeypatch.setitem(sys.modules, "langfuse", mod)

    tracing.init(config=_Cfg(enabled=True, public_key="pk", secret_key="sk"))

    assert tracing.is_enabled() is False


def test_server_inits_tracing_with_the_loaded_config():
    """Wiring lock: tracing.init must be called WITH the config, and after it loads.

    The whole bug was that init ran in the early observability block, before any
    config existed — so the config fallback could never be read. server/__init__.py
    is only importable inside a full boot, so lock the source contract (the same way
    the shutdown-flush test above does).
    """
    from pathlib import Path

    src = (Path(__file__).parents[1] / "server" / "__init__.py").read_text()
    assert "tracing.init(config=STATE.graph_config)" in src, (
        "tracing.init no longer receives the loaded config — the config fallback is dead again"
    )
    # It must come AFTER the config is loaded. _init_langgraph_agent is what sets
    # STATE.graph_config, so the call has to sit below that line.
    assert src.index("_init_langgraph_agent(headless_setup=headless_setup)") < src.index(
        "tracing.init(config=STATE.graph_config)"
    ), "tracing.init runs before the config loads — it can only see the environment there"


# ── console reachability (the half the issue was actually about) ──────────────


def _console_source(*parts: str) -> str:
    from pathlib import Path

    return (Path(__file__).parents[1] / "apps" / "web" / "src" / Path(*parts)).read_text()


def test_tracing_fields_are_grouped_for_a_console_section_that_exists():
    """The four fields must land in a category the console actually renders.

    A field's `section` decides its `category` (graph/settings_schema.py `_SECTION_CATEGORY`),
    and the console renders a category only where some surface names it — either a
    `SettingsCategoryPanel category=…` or a `QuickSetting` chip listing the keys. A field in a
    category nobody names reaches `/api/settings` and no DOM, which is #3017's failure mode
    (something is off and nothing says so) reproduced one layer up.
    """
    from graph.config import LangGraphConfig
    from graph.settings_schema import build_schema

    groups = build_schema(LangGraphConfig())
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    section_cat = {g["section"]: g["category"] for g in groups}

    for key in ("tracing.enabled", "tracing.host", "tracing.public_key", "tracing.secret_key"):
        f = by_key[key]
        assert f["section"] == "Tracing", f"{key} moved out of the section the console renders"
        # Agent-scoped is what forces the section out of Box: see the next test.
        assert f["scope"] == "agent", f"{key} is a per-agent credential (ADR 0047 D5)"
        assert f["restart"] is True, f"{key} is read once at boot — the badge must say so"
    assert section_cat["Tracing"] == "Observability"
    # And NOT the host-console-only Box domain the telemetry rollup lives in.
    assert section_cat["Telemetry"] == "Box"


def test_tracing_is_reachable_from_a_fleet_members_console():
    """#3017 acceptance: a desktop-launched member can be switched on from a console.

    That member (`protoagent-server --port … --ui none`) serves no `/app` of its own, so the
    only console that sees it is the hub's slug-scoped member window — where every `hostOnly:`
    section is dropped (apps/web/src/settings/sectionGate.ts). Box ▸ Telemetry is `hostOnly`,
    so a Settings home filed there would have been exactly as unreachable as the environment
    variables the issue is about. The Agent-group "Tracing" section is the fix, and this pins
    the two properties that make it work: it renders the right category, and it carries no
    host gate. The console-side behaviour of the filter itself is covered by
    apps/web/src/settings/tracingSectionGate.test.ts.
    """
    import re

    # The section TABLE (ids + gates) is a leaf, `settings/sections.ts`, so ⌘K and the desktop
    # Launcher can name a section without importing the whole settings tree; SettingsSurface.tsx
    # keeps the render functions. The two halves are asserted against their two files.
    table = _console_source("settings", "sections.ts")
    surface = _console_source("settings", "SettingsSurface.tsx")
    section = re.search(r'\{[^{}]*id: "tracing"[^{}]*\}', table)
    assert section, "Settings has no 'tracing' section — the fields render in no console surface"
    body = section.group(0)
    assert 'tracing: () => <SettingsCategoryPanel category="Observability"' in surface, (
        "the Tracing section renders some other category"
    )
    assert "hostOnly" not in body, (
        "the Tracing section is host-console-only again — a fleet member (the deployment shape "
        "#3017 exists for) can no longer reach it"
    )
    # The contrast that makes the point, and the reason this section is not simply folded in
    # beside the telemetry rollup.
    telemetry_section = re.search(r'\{[^{}]*id: "telemetry"[^{}]*\}', table)
    assert telemetry_section and "hostOnly: true" in telemetry_section.group(0)


def test_telemetry_surface_chips_the_tracing_keys():
    """The Trace column's "off" cell sends the operator to a control, so one must be there.

    On the host console that control is the gear beside the telemetry table. It has to list
    every key the setup needs — a chip carrying only the toggle would open a dialog that can't
    finish the job, since `tracing.enabled` alone does nothing without the key pair.
    """
    src = _console_source("telemetry", "TelemetrySurface.tsx")
    for key in ("tracing.enabled", "tracing.host", "tracing.public_key", "tracing.secret_key"):
        assert f'"{key}"' in src, f"the telemetry surface no longer offers {key}"
    # The cell's title has to name a section that renders these fields.
    assert "Settings ▸ Tracing" in src, (
        "the disabled-trace cell points at a settings path that does not render tracing"
    )


def test_telemetry_surface_chips_every_telemetry_section_field():
    """#3032: a Telemetry schema field must not exist in the API but in no console DOM.

    Pin the schema inventory to the contextual surface rather than repeating a static key
    list: adding another field to this section now fails until its operator control ships.
    """
    from graph.config import LangGraphConfig
    from graph.settings_schema import build_schema

    src = _console_source("telemetry", "TelemetrySurface.tsx")
    telemetry_fields = [
        field
        for group in build_schema(LangGraphConfig())
        for field in group["fields"]
        if field["section"] == "Telemetry"
    ]

    assert {field["key"] for field in telemetry_fields} == {
        "telemetry.fleet_trace_export",
        "telemetry.enabled",
        "telemetry.retention_days",
        "prompts.capture",
        "prompts.retention_days",
        "prompts.max_calls",
    }
    for field in telemetry_fields:
        assert f'"{field["key"]}"' in src, f'{field["key"]} renders in no Telemetry control'


# ── shutdown flush wiring ─────────────────────────────────────────────────────


def test_flush_delegates_to_client_and_swallows_errors():
    tracing = _reload_tracing()
    fake, _span, _child = _enable_with_fake_client(tracing)
    tracing.flush()
    fake.flush.assert_called_once()
    fake.flush.side_effect = RuntimeError("exporter down")
    tracing.flush()  # must not raise


def test_server_shutdown_hook_flushes_tracing():
    """Wiring lock: the server shutdown hook must flush buffered observations
    so spans survive process exit (server/__init__.py is only importable inside
    a full boot, so we lock the source contract)."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "server" / "__init__.py").read_text()
    hook = src.split('@fastapi_app.on_event("shutdown")', 1)[1]
    hook = hook.split("register_chat_routes", 1)[0]  # the hook body ends before route registration
    assert "tracing.flush" in hook, "shutdown hook no longer flushes Langfuse tracing"


def test_no_legacy_shims_exist():
    """Greenfield guarantee — start_trace / end_trace / trace_llm_call were
    removed. Their return would silently break the nesting contract by
    teaching callers to bypass trace_session."""
    tracing = _reload_tracing()
    assert not hasattr(tracing, "start_trace")
    assert not hasattr(tracing, "end_trace")
    assert not hasattr(tracing, "trace_llm_call")


def test_otel_cross_context_detach_error_is_silenced():
    """When an SSE consumer (e.g. an A2A executor) closes
    the stream early, GeneratorExit propagates through
    trace_session's __aexit__. The Langfuse span's underlying OTel
    token was attached in a child task's contextvar snapshot, so the
    detach during cleanup logs an error before raising. Our finally
    block already swallows the raised ValueError — this test locks in
    that the OTel logger doesn't spam docker logs about it either.
    """
    import io
    import logging

    _reload_tracing()  # ensures the filter is installed via module import

    handler_buf = io.StringIO()
    handler = logging.StreamHandler(handler_buf)
    handler.setLevel(logging.ERROR)
    otel_log = logging.getLogger("opentelemetry.context")
    otel_log.addHandler(handler)
    otel_log.setLevel(logging.ERROR)

    try:
        # Simulate the exact noise OTel emits on cross-context detach.
        # OTel calls `_logger.error("Failed to detach context", exc_info=True)` —
        # the actual ValueError text is in exc_info, not the message. Filter
        # has to match on the message string itself.
        try:
            raise ValueError(
                "<Token var=<ContextVar name='current_context'> at 0x...> was created in a different Context"
            )
        except ValueError:
            otel_log.error("Failed to detach context", exc_info=True)
        otel_log.error("Some other unrelated OTel error that should NOT be silenced")
    finally:
        otel_log.removeHandler(handler)

    output = handler_buf.getvalue()
    assert "Failed to detach context" not in output, "filter failed to silence the cross-context detach error"
    assert "unrelated OTel error" in output, "filter is too broad — it silenced an unrelated error too"
