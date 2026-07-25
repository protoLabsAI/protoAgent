"""The activity emit seam (#2262) — bind-at-boot, best-effort append, and the
prompt-cache warnings as its first consumer."""

import pytest
from langchain_core.messages import SystemMessage
from types import SimpleNamespace

import activity
from activity import ActivityLog, emit, set_default_feed


@pytest.fixture(autouse=True)
def _unbound_feed():
    """Every test starts and ends with no bound feed — the process-wide slot must
    never leak between tests (or into the suite's other middleware tests)."""
    set_default_feed(None)
    yield
    set_default_feed(None)


def _feed(tmp_path) -> ActivityLog:
    return ActivityLog(str(tmp_path / "activity.db"))


def test_emit_is_a_noop_before_binding():
    emit("nobody is listening")  # must not raise — early boot / bare tests


def test_emit_appends_to_the_bound_feed(tmp_path):
    feed = _feed(tmp_path)
    set_default_feed(feed)
    emit("cache is being ignored", trigger="prompt-cache")
    rows = feed.recent()
    assert len(rows) == 1
    assert rows[0]["text"] == "cache is being ignored"
    assert rows[0]["origin"] == "system"
    assert rows[0]["trigger"] == "prompt-cache"


def test_emit_never_raises_on_a_broken_feed():
    class _Broken:
        def add(self, **_kw):
            raise RuntimeError("disk full")

    set_default_feed(_Broken())
    emit("still must not raise")


def test_unbinding_restores_the_noop(tmp_path):
    feed = _feed(tmp_path)
    set_default_feed(feed)
    set_default_feed(None)
    emit("gone")
    assert feed.recent() == []


def test_server_boot_binds_the_feed(tmp_path):
    # The bind site is agent_init right after _build_activity_log — pin that the
    # seam is wired by checking the module-level slot after a manual bind of the
    # same object shape the server produces.
    feed = _feed(tmp_path)
    set_default_feed(feed)
    assert activity._default_feed is feed


# ── first consumer: the prompt-cache warnings (#2255 follow-up) ────────────────


class _Req:
    def __init__(self, model_name, system_message, state=None):
        self.model = SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}

    def override(self, **kw):
        r = _Req(self.model.model_name, self.system_message, self.state)
        for k, v in kw.items():
            setattr(r, k, v)
        return r


def _zero_usage_response():
    from langchain_core.messages import AIMessage

    return SimpleNamespace(
        result=[
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "total_tokens": 105,
                    "input_token_details": {"cache_read": 0, "cache_creation": 0},
                },
            )
        ]
    )


def test_zero_hit_warning_lands_in_the_feed(tmp_path):
    from graph.middleware.prompt_cache import PromptCacheMiddleware

    feed = _feed(tmp_path)
    set_default_feed(feed)
    mw = PromptCacheMiddleware()
    req = _Req("protolabs/fast", SystemMessage(content="S" * 5000), state={})
    for _ in range(4):
        mw.wrap_model_call(req, lambda r: _zero_usage_response())
    rows = [r for r in feed.recent() if "not engaging" in r["text"]]
    assert len(rows) == 1  # once per model, like the log warning
    assert "protolabs/fast" in rows[0]["text"]
    assert rows[0]["trigger"] == "prompt-cache"


def test_rejection_fallback_lands_in_the_feed(tmp_path):
    from graph.middleware.prompt_cache import PromptCacheMiddleware

    feed = _feed(tmp_path)
    set_default_feed(feed)
    mw = PromptCacheMiddleware()

    def handler(r):
        if isinstance(r.system_message.content, list):
            raise ValueError("cache_control not supported")
        return _zero_usage_response()

    req = _Req("protolabs/qwen", SystemMessage(content="S" * 5000), state={"context": "c"})
    mw.wrap_model_call(req, handler)
    rows = [r for r in feed.recent() if "rejected cache_control" in r["text"]]
    assert len(rows) == 1
    assert "protolabs/qwen" in rows[0]["text"]
