"""Operator-route error mapping, availability and input validation (#3973).

Each test below failed on main before the fix:

1. ``_http_error`` echoed ``str(exc)`` as a 500's detail (internal paths, library text).
2. A missing scheduler job / task answered 400, 500 or a 200 ``{deleted|canceled: false}``
   instead of 404.
3. ``/api/runtime/status``, ``/api/goals/{id}`` and ``/api/subagents`` had no error guard.
4. ``POST /api/chat`` answered a failed turn with a bare 500 (raised) or a 200 (in-band).
5. One malformed bus event ended the ``/api/events`` SSE stream.
6. Unbounded / untyped input: subagent batch, ``?status=``, background job ids,
   ``POST /api/goals``, a goal spec's budgets, a schedule / timezone.
7. Task routes with no store wired: AttributeError 500s and ``initialized: true``.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.routes import MAX_BATCH_TASKS, _sse_event_stream, register_operator_routes


def _app(**kw) -> TestClient:
    app = FastAPI()
    base = dict(
        runtime_status=lambda: {"graph_loaded": True},
        subagent_list=lambda: [],
        subagent_run=lambda req: None,
        subagent_batch=lambda req: None,
    )
    base.update(kw)
    register_operator_routes(app, **base)
    return TestClient(app, raise_server_exceptions=False)


# --- 1. a 500 never leaks the exception text ------------------------------------------


def test_500_detail_is_generic_with_an_error_id_and_the_exception_is_logged(caplog):
    async def _boom():
        raise RuntimeError("/Users/secret/protoagent/scheduler.db: disk I/O error")

    client = _app(scheduler_list=_boom)
    with caplog.at_level(logging.ERROR, logger="operator_api.routes"):
        resp = client.get("/api/scheduler/jobs")

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "/Users/secret" not in detail and "disk I/O" not in detail
    assert "error id" in detail
    error_id = detail.split("error id ")[1][:8]
    # The server log carries the id AND the real exception, so the operator can find it.
    logged = [r for r in caplog.records if error_id in r.getMessage()]
    assert logged and logged[0].exc_info and "disk I/O error" in str(logged[0].exc_info[1])


def test_400_and_409_keep_their_own_message():
    async def _bad(_req):
        raise ValueError("prompt is required")

    async def _not_loaded(_req):
        raise RuntimeError("scheduler is not loaded")

    assert _app(scheduler_add=_bad).post(
        "/api/scheduler/jobs", json={"prompt": "", "schedule": "0 9 * * *"}
    ).json() == {"detail": "prompt is required"}
    r = _app(scheduler_add=_not_loaded).post("/api/scheduler/jobs", json={"prompt": "p", "schedule": "0 9 * * *"})
    assert r.status_code == 409 and "not loaded" in r.json()["detail"]


# --- 2. a missing resource is a 404 ---------------------------------------------------


@pytest.fixture
def real_scheduler_client(tmp_path, monkeypatch):
    """The REAL console handlers + routes over a real LocalScheduler."""
    import runtime.state as rs
    from operator_api import console_handlers as ch
    from scheduler.local import LocalScheduler

    sched = LocalScheduler("t", invoke_url="http://127.0.0.1:1", db_dir=tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", sched, raising=False)
    client = _app(
        scheduler_list=ch._operator_scheduler_list,
        scheduler_add=ch._operator_scheduler_add,
        scheduler_cancel=ch._operator_scheduler_cancel,
        scheduler_update=ch._operator_scheduler_update,
    )
    return client, sched


def test_delete_missing_scheduler_job_is_404(real_scheduler_client):
    client, _ = real_scheduler_client
    resp = client.delete("/api/scheduler/jobs/nope")
    assert resp.status_code == 404, resp.text
    assert "nope" in resp.json()["detail"]


def test_delete_existing_scheduler_job_still_200(real_scheduler_client):
    client, sched = real_scheduler_client
    sched.add_job("p", "0 9 * * *", job_id="j1")
    assert client.delete("/api/scheduler/jobs/j1").json() == {"canceled": True}


def test_put_missing_scheduler_job_is_404(real_scheduler_client):
    client, _ = real_scheduler_client
    resp = client.put("/api/scheduler/jobs/nope", json={"prompt": "p", "schedule": "0 9 * * *"})
    assert resp.status_code == 404, resp.text


def test_cancel_that_fails_on_an_existing_job_is_not_reported_as_404(real_scheduler_client, monkeypatch):
    # LocalScheduler.cancel_job swallows a store error as False; the job is still there,
    # so "not found" would be a lie — it's a 500.
    client, sched = real_scheduler_client
    sched.add_job("p", "0 9 * * *", job_id="j1")
    monkeypatch.setattr(sched, "cancel_job", lambda job_id: False)
    assert client.delete("/api/scheduler/jobs/j1").status_code == 500


@pytest.fixture
def real_tasks_client(tmp_path):
    from tasks.store import TaskStore

    store = TaskStore(db_path=str(tmp_path / "tasks.db"))
    return _app(tasks_store=store), store


def test_patch_missing_task_is_404(real_tasks_client):
    client, _ = real_tasks_client
    resp = client.patch("/api/tasks/issues/nope-1", json={"status": "in_progress"})
    assert resp.status_code == 404, resp.text
    assert "nope-1" in resp.json()["detail"]


def test_close_missing_task_is_404(real_tasks_client):
    client, _ = real_tasks_client
    assert client.post("/api/tasks/issues/nope-1/close", json={}).status_code == 404


def test_delete_missing_task_is_404_and_existing_is_200(real_tasks_client):
    client, store = real_tasks_client
    issue = store.create("x")
    assert client.delete(f"/api/tasks/issues/{issue['id']}").json() == {"deleted": True}
    resp = client.delete(f"/api/tasks/issues/{issue['id']}")  # already gone
    assert resp.status_code == 404, resp.text


# --- 3. unguarded routes -------------------------------------------------------------


def _boom():
    raise OSError("/private/var/secret: permission denied")


def _assert_guarded(resp):
    assert resp.status_code == 500
    detail = resp.json()["detail"]  # a JSON error, not starlette's bare text 500
    assert "error id" in detail and "/private/var/secret" not in detail


def test_runtime_status_is_guarded():
    _assert_guarded(_app(runtime_status=_boom).get("/api/runtime/status"))


def test_async_runtime_status_is_guarded():
    async def _aboom():
        _boom()

    _assert_guarded(_app(runtime_status=_aboom).get("/api/runtime/status"))


def test_subagent_list_is_guarded():
    _assert_guarded(_app(subagent_list=_boom).get("/api/subagents"))


def test_goal_status_is_guarded(monkeypatch):
    import runtime.state as rs

    class _Store:
        def get(self, _sid):
            _boom()

    class _Ctrl:
        store = _Store()

    monkeypatch.setattr(rs.STATE, "goal_controller", _Ctrl(), raising=False)
    _assert_guarded(_app().get("/api/goals/chat-1"))


# --- 4. POST /api/chat maps a failed turn like /v1 -----------------------------------


def _chat_client(monkeypatch, *, raises=None, reply=None):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id, **_kw):
        if raises is not None:
            raise raises
        return reply

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app, raise_server_exceptions=False)


class _UpstreamError(Exception):
    def __init__(self, status):
        super().__init__(f"provider said {status}")
        self.status_code = status


@pytest.mark.parametrize(("upstream", "status"), [(429, 429), (401, 502), (500, 502)])
def test_api_chat_maps_a_raised_upstream_failure(monkeypatch, upstream, status):
    resp = _chat_client(monkeypatch, raises=_UpstreamError(upstream)).post(
        "/api/chat", json={"message": "hi", "session_id": "s1"}
    )
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["upstream_status"] == upstream and detail["session_id"] == "s1"


def test_api_chat_maps_an_unreachable_gateway_to_502(monkeypatch):
    exc = RuntimeError("wrapped")
    exc.__cause__ = ConnectionRefusedError("refused")
    assert _chat_client(monkeypatch, raises=exc).post("/api/chat", json={"message": "hi"}).status_code == 502


def test_api_chat_internal_failure_is_generic_500(monkeypatch):
    resp = _chat_client(monkeypatch, raises=KeyError("/Users/secret/thing")).post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 500
    message = resp.json()["detail"]["message"]
    assert "error id" in message and "/Users/secret" not in message


@pytest.mark.parametrize(
    ("err", "status"),
    [
        ({"message": "rate limited", "type": "rate_limit_error", "upstream_status": 429}, 429),
        ({"message": "bad key", "type": "authentication_error", "upstream_status": 401}, 502),
        ({"message": "gw down", "type": "server_error", "upstream_status": None, "upstream_unreachable": True}, 502),
        ({"message": "unknown model", "type": "invalid_request_error", "upstream_status": None}, 400),
        (
            {
                "message": "sign-in refresh failed",
                "type": "server_error",
                "upstream_status": None,
                "model_unavailable": True,
                "retry_after": 30,
            },
            503,
        ),
    ],
)
def test_api_chat_maps_an_in_band_turn_error(monkeypatch, err, status):
    reply = [{"role": "assistant", "content": f"**Error:** {err['message']}", "error": err}]
    resp = _chat_client(monkeypatch, reply=reply).post("/api/chat", json={"message": "hi"})
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["message"] == err["message"]
    if status == 503:
        assert resp.headers["retry-after"] == "30"


def test_api_chat_success_unchanged(monkeypatch):
    reply = [{"role": "assistant", "content": "hello"}]
    body = _chat_client(monkeypatch, reply=reply).post("/api/chat", json={"message": "hi", "session_id": "s1"}).json()
    assert body == {"response": "hello", "messages": reply, "session_id": "s1"}


# --- 5. one malformed bus event doesn't end the SSE stream ----------------------------


async def test_sse_stream_skips_a_malformed_event_and_keeps_going():
    events = [
        {"seq": 1, "event": "a.one", "data": {"n": 1}},
        {"seq": 2, "data": {"no": "topic"}},  # missing `event`
        {"seq": 3, "event": "a.bad", "data": {"x": object()}},  # json can't encode it
        "not-a-dict",
        {"seq": 4, "event": "a.two", "data": {"n": 2}},
    ]

    async def _subscribe(since=None):
        for e in events:
            yield e

    frames = [f async for f in _sse_event_stream(_subscribe, keepalive_s=5)]
    text = "".join(frames)
    assert '"topic": "a.one"' in text
    assert '"topic": "a.two"' in text  # the stream survived the bad ones
    assert "a.bad" not in text


# --- 6. input validation -------------------------------------------------------------


def _batch_client(seen):
    async def _batch(req):
        seen.append(req)
        return "ok"

    return _app(subagent_batch=_batch)


def test_subagent_batch_is_capped():
    seen: list = []
    client = _batch_client(seen)
    over = [{"prompt": f"p{i}"} for i in range(MAX_BATCH_TASKS + 1)]
    assert client.post("/api/subagents/batch", json={"tasks": over}).status_code == 422
    assert client.post("/api/subagents/batch", json={"tasks": over[:MAX_BATCH_TASKS]}).status_code == 200
    assert len(seen) == 1


@pytest.mark.parametrize("task", [{"description": "no prompt"}, {"prompt": ""}, {"prompt": ["x"]}, "just-a-string"])
def test_subagent_batch_items_are_typed(task):
    seen: list = []
    assert _batch_client(seen).post("/api/subagents/batch", json={"tasks": [task]}).status_code == 422
    assert seen == []


def test_subagent_batch_payload_keeps_the_runner_defaults():
    seen: list = []
    _batch_client(seen).post(
        "/api/subagents/batch", json={"tasks": [{"prompt": "a"}, {"prompt": "b", "type": "coder", "description": "d"}]}
    )
    # Unset keys are dropped, so the runner's own `type` default still applies.
    assert seen[0]["tasks"] == [
        {"prompt": "a", "description": ""},
        {"prompt": "b", "description": "d", "type": "coder"},
    ]


class _BgStore:
    def __init__(self):
        self.calls: list = []

    def list(self, **kw):
        self.calls.append(("list", kw))
        return []

    def dismiss(self, job_id):
        self.calls.append(("dismiss", job_id))
        return True


class _BgMgr:
    def __init__(self):
        self.store = _BgStore()
        self.canceled: list = []

    async def cancel(self, job_id):
        self.canceled.append(job_id)
        return {"ok": True}


@pytest.fixture
def bg(monkeypatch):
    import runtime.state as rs

    mgr = _BgMgr()
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)
    return _app(), mgr


def test_background_status_filter_is_checked(bg):
    client, mgr = bg
    resp = client.get("/api/background?status=bogus")
    assert resp.status_code == 400 and "running" in resp.json()["detail"]
    assert mgr.store.calls == []
    for ok in ("running", "completed", "failed", "canceled", ""):
        assert client.get(f"/api/background?status={ok}").status_code == 200


def test_background_delete_and_cancel_validate_the_job_id(bg):
    client, mgr = bg
    assert client.delete("/api/background/not-a-job").status_code == 400
    assert client.post("/api/background/not-a-job/cancel").status_code == 400
    assert mgr.store.calls == [] and mgr.canceled == []
    good = "bg-0123456789ab"
    assert client.delete(f"/api/background/{good}").json() == {"ok": True, "deleted": True}
    assert client.post(f"/api/background/{good}/cancel").json() == {"ok": True}


def _goal_client(seen):
    async def _set(body):
        seen.append(body)
        return {"ok": True, "message": "set"}

    return _app(goal_set=_set)


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": "s", "condition": "c", "max_iterations": "abc"},
        {"session_id": "s", "condition": "c", "max_iterations": 0},
        {"session_id": "s", "condition": "c", "max_iterations": 1.5},
        {"session_id": "s", "condition": "c", "no_progress_limit": -1},
        {"session_id": "s", "condition": "c", "verifier": ["command"]},
        {"session_id": "s", "condition": "c", "kick": "maybe"},
    ],
)
def test_goal_set_body_is_typed(body):
    seen: list = []
    assert _goal_client(seen).post("/api/goals", json=body).status_code == 422
    assert seen == []


def test_goal_set_wire_shape_is_backwards_compatible():
    seen: list = []
    client = _goal_client(seen)
    minimal = {"session_id": "s", "condition": "c", "verifier": {"type": "llm"}}
    assert client.post("/api/goals", json=minimal).status_code == 200
    # Only the keys sent reach the handler (its own defaults — kick=True — still apply),
    # a string constraint is still accepted, and unknown keys are ignored as before.
    full = {**minimal, "max_iterations": 5, "constraints": "one", "kick": False, "extra": 1}
    assert client.post("/api/goals", json=full).status_code == 200
    assert seen == [minimal, {**minimal, "max_iterations": 5, "constraints": "one", "kick": False}]


def _sched_client(seen):
    async def _add(req):
        seen.append(req)
        return {"id": "j"}

    async def _update(job_id, req):
        seen.append(req)
        return {"id": job_id}

    return _app(scheduler_add=_add, scheduler_update=_update)


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": "p", "schedule": "61 * * * *"},
        {"prompt": "p", "schedule": "every tuesday"},
        {"prompt": "p", "schedule": "   "},
        {"prompt": "p", "schedule": "2026-13-45T25:00"},
        {"prompt": "p", "schedule": "0 9 * * *", "timezone": "Not/AZone"},
    ],
)
def test_schedule_is_format_checked_at_the_route(body):
    seen: list = []
    client = _sched_client(seen)
    resp = client.post("/api/scheduler/jobs", json=body)
    assert resp.status_code == 400, resp.text
    assert "invalid" in resp.json()["detail"] or "empty" in resp.json()["detail"]
    upd = {k: v for k, v in body.items() if k != "prompt"}
    assert client.put("/api/scheduler/jobs/j", json=upd).status_code == 400
    assert seen == []


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": "p", "schedule": "0 9 * * 1-5", "timezone": "America/Chicago"},
        {"prompt": "p", "schedule": "2026-12-01T09:00:00+00:00"},
        {"prompt": "p", "schedule": "2026-12-01T09:00"},
        {"prompt": "p", "schedule": "*/5 * * * *", "timezone": ""},
    ],
)
def test_well_formed_schedules_pass_the_route(body):
    seen: list = []
    assert _sched_client(seen).post("/api/scheduler/jobs", json=body).status_code == 200
    assert len(seen) == 1


def test_partial_update_without_schedule_is_not_checked():
    seen: list = []
    client = _sched_client(seen)
    assert client.put("/api/scheduler/jobs/j", json={"prompt": "new"}).status_code == 200
    assert client.put("/api/scheduler/jobs/j", json={"timezone": None}).status_code == 200


@pytest.fixture
def goal_ctrl(tmp_path):
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore

    return GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))


@pytest.mark.parametrize(
    "extra",
    [
        '"max_iterations": "abc"',
        '"max_iterations": 0',
        '"max_iterations": true',
        '"max_iterations": 1000000',
        '"no_progress_limit": "3"',
        '"no_progress_limit": 2.5',
    ],
)
def test_goal_json_spec_budgets_are_type_and_range_checked(goal_ctrl, extra):
    msg = asyncio.run(goal_ctrl.parse_control('/goal {"condition": "c", ' + extra + "}", "s"))
    assert msg.startswith("Could not set goal:"), msg
    assert goal_ctrl.active_goal("s") is None


def test_goal_json_spec_with_valid_budgets_still_sets(goal_ctrl):
    asyncio.run(goal_ctrl.parse_control('/goal {"condition": "c", "max_iterations": 4, "no_progress_limit": 2}', "s"))
    state = goal_ctrl.active_goal("s")
    assert state is not None and state.max_iterations == 4 and state.no_progress_limit == 2


def test_programmatic_goal_set_budgets_are_checked(goal_ctrl):
    ok, msg = goal_ctrl.set_goal_operator("s", "c", {"type": "llm"}, max_iterations="abc")
    assert ok is False and "max_iterations" in msg
    ok, msg = goal_ctrl.set_goal_safe("s", "c", {"type": "plugin", "check": "x:y"}, no_progress_limit=0)
    assert ok is False and "no_progress_limit" in msg
    assert goal_ctrl.active_goal("s") is None


# --- 7. task routes without a store --------------------------------------------------


def test_task_routes_without_a_store_answer_503_and_status_reports_uninitialized():
    client = _app()  # no tasks_store
    assert client.get("/api/tasks/status").json() == {"initialized": False}
    for method, path, body in [
        ("post", "/api/tasks/init", {}),
        ("get", "/api/tasks/issues", None),
        ("post", "/api/tasks/issues", {"title": "t"}),
        ("patch", "/api/tasks/issues/x-1", {"status": "closed"}),
        ("post", "/api/tasks/issues/x-1/close", {}),
        ("delete", "/api/tasks/issues/x-1", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 503, (method, path, resp.text)
        assert resp.json()["detail"] == "tasks not enabled"


def test_api_chat_in_band_500_keeps_the_turns_own_message(monkeypatch, caplog):
    # An in-band failure's message is already user-facing (server.chat._fail_turn wrote
    # it and stored it in the transcript); /api/chat must not mask it as "internal".
    msg = "the turn produced no reply — it may have stalled or been interrupted; retry it"
    err = {"message": msg, "type": "server_error", "upstream_status": None, "exception": None}
    reply = [{"role": "assistant", "content": f"**Error:** {msg}", "error": err}]
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        resp = _chat_client(monkeypatch, reply=reply).post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["message"] == msg
    # The error id the client sees is in the server log beside the real message.
    assert any(detail["error_id"] in r.getMessage() and msg in r.getMessage() for r in caplog.records)


def test_api_chat_raised_500_is_masked_but_logged_with_its_id(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="protoagent.server"):
        resp = _chat_client(monkeypatch, raises=KeyError("/Users/secret/thing")).post(
            "/api/chat", json={"message": "hi"}
        )
    detail = resp.json()["detail"]
    assert resp.status_code == 500 and "/Users/secret" not in detail["message"]
    assert detail["error_id"] in detail["message"]
    assert any(detail["error_id"] in r.getMessage() and "/Users/secret" in r.getMessage() for r in caplog.records)


def test_provider_closed_stream_is_a_502_not_an_internal_500(monkeypatch):
    # turn_error flags a provider stream drop (reconnects exhausted); both /v1 and
    # /api/chat answer it as the failed upstream hop it is.
    from graph.llm import StreamStallTimeout
    from operator_api.chat_routes import _turn_error_status
    from server.chat import _PROVIDER_CLOSED_MSG, turn_error

    err = turn_error(StreamStallTimeout("no first token"), _PROVIDER_CLOSED_MSG)
    assert err["upstream_stream_closed"] is True
    assert _turn_error_status(err)[0] == 502
    reply = [{"role": "assistant", "content": f"**Error:** {_PROVIDER_CLOSED_MSG}", "error": err}]
    resp = _chat_client(monkeypatch, reply=reply).post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 502 and resp.json()["detail"]["message"] == _PROVIDER_CLOSED_MSG
    assert "upstream_stream_closed" not in turn_error(RuntimeError("bug"))
