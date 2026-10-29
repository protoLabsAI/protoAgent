"""Draft/save integration over real telemetry, task transcripts and instance files."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from a2a.server.tasks.database_task_store import Base, TaskModel

from evals.runner import load_instance_cases
from observability.telemetry_store import TelemetryStore
from operator_api.eval_case_routes import register_eval_case_routes
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive

PROMPT = "  Find my notes.\nKeep the original wording — please.  "


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    from infra.paths import reset_instance_paths

    reset_instance_paths()
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(STATE, "telemetry_store", store)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/tasks.db")
    monkeypatch.setattr(STATE, "a2a_task_engine", engine)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for task, trace, prompt, incognito in [
                ("t1", "tr1", PROMPT, False),
                ("t2", "tr2", "second", False),
                ("t3", "tr3", "private", True),
            ]:
                await conn.execute(
                    TaskModel.__table__.insert(),
                    {
                        "id": task,
                        "context_id": "s1",
                        "kind": "task",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [],
                        "history": [
                            {"role": "ROLE_USER", "parts": [{"text": prompt}], "metadata": {"incognito": incognito}}
                        ],
                    },
                )
                store.record(
                    {
                        "task_id": task,
                        "session_id": "s1",
                        "trace_id": trace,
                        "created_at": f"2026-10-01T00:00:0{task[-1]}+00:00",
                        "state": "completed",
                    }
                )
                store.set_feedback(session_id="s1", trace_id=trace, value=1, note="good", created_at="2026-10-01")

    asyncio.run(seed())
    entries = [
        {"ts": "2026-10-01", "session_id": sid, "trace_id": trace, "tool": tool, "success": True}
        for sid, trace, tool in [
            ("s1", "tr1", "search"),
            ("s1", "tr1", "read"),
            ("s1", "tr1", "read"),
            ("s1", "tr2", "other"),
            ("s2", "tr1", "foreign"),
        ]
    ]
    (tmp_path / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    app = FastAPI()
    register_eval_case_routes(app)
    with TestClient(app) as client:
        yield client, store, tmp_path
    asyncio.run(engine.dispose())


def draft(client, **kwargs):
    return client.post("/api/evals/cases/draft", json={"session_id": "s1", "trace_id": "tr1", **kwargs})


def case():
    return {"id": "sample", "name": "A case", "category": "instance", "prompt": PROMPT, "kind": "ask"}


def test_up_draft_is_verbatim_and_writes_nothing(env):
    client, _, root = env
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    response = draft(client)
    assert response.status_code == 200
    value = response.json()
    assert value["prompt"] == PROMPT
    assert value["expected_tools"] == ["read", "search"]
    assert value["tier"] == "gate"
    assert value["source_trace_id"] == "tr1"
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert draft(client, trace_id="tr2").json()["prompt"] == "second"


@pytest.mark.parametrize("rating,friction", [(0, False), (None, True), (1, True), (0, True)])
def test_negative_or_friction_has_no_expectations(env, rating, friction):
    client, store, root = env
    store.delete_feedback(session_id="s1", trace_id="tr1")
    if rating is not None:
        store.set_feedback(session_id="s1", trace_id="tr1", value=rating, note="Wrong answer", created_at="now")
    if friction:
        (root / "friction.jsonl").write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {"session_id": "s1", "trace_id": "tr1", "summary": "Search failed"},
                    {"session_id": "s1", "trace_id": "tr2", "summary": "Unrelated"},
                ]
            )
        )
    response = draft(client)
    assert response.status_code == 200
    value = response.json()
    assert not any(k in value for k in ("expected_tools", "forbidden_tools", "expected_patterns", "verify_rubric"))
    assert value["prompt"] == PROMPT
    assert value["source_trace_id"] == "tr1"
    assert value["comment"] == "\n".join(
        (["Wrong answer"] if rating is not None else []) + (["Search failed"] if friction else [])
    )
    assert not (root / "instance/config/evals/cases.json").exists()


def test_save_requires_assertion_appends_and_loader_reads_it(env):
    client, store, root = env
    store.set_feedback(session_id="s1", trace_id="tr1", value=0, note="Needs a fix", created_at="now")
    value = draft(client).json()
    path = root / "instance/config/evals/cases.json"
    response = client.post("/api/evals/cases", json={"case": value})
    assert response.status_code == 422
    assert "assert something" in response.json()["detail"]
    assert not path.exists()
    value["expected_tools"] = ["search"]
    assert client.post("/api/evals/cases", json={"case": value}).status_code == 201
    other = {**value, "id": "other", "forbidden_patterns": ["invented"]}
    assert client.post("/api/evals/cases", json={"case": other}).status_code == 201
    assert json.loads(path.read_text()) == [value, other]
    loaded = load_instance_cases(path)
    assert [c["id"] for c in loaded] == [f"instance:{value['id']}", "instance:other"]
    assert loaded[0]["prompt"] == PROMPT
    assert loaded[0]["expected_tools"] == ["search"]
    before = path.read_bytes()
    assert client.post("/api/evals/cases", json={"case": value}).status_code == 409
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        {"expected_tools": "search"},
        {"expected_tools": [""]},
        {"forbidden_tools": []},
        {"verify_rubric": {}},
        {"verify_rubric": {"criteria": [""], "threshold": 1}},
        {"verify_rubric": {"criteria": ["correct"], "threshold": 0}},
        {"expected_tools": ["search"], "tier": "typo"},
        {"expected_tools": ["search"], "kind": "agent_card"},
        {"expected_tools": ["search"], "prompt": ""},
    ],
)
def test_invalid_cases_are_refused(env, extra):
    client, _, root = env
    assert client.post("/api/evals/cases", json={"case": {**case(), **extra}}).status_code == 422
    assert not (root / "instance/config/evals/cases.json").exists()


@pytest.mark.parametrize(
    "assertion",
    [
        {"expected_tools": []},
        {"forbidden_tools": ["search"]},
        {"expected_patterns": ["answer"]},
        {"forbidden_patterns": ["wrong"]},
        {"verify_rubric": {"criteria": ["Answers correctly"]}},
    ],
)
def test_supported_assertions(env, assertion):
    client, _, _ = env
    assert client.post("/api/evals/cases", json={"case": {**case(), **assertion}}).status_code == 201


def test_incognito_and_unknown_turns_refused(env):
    client, _, _ = env
    assert draft(client, incognito=True).status_code == 409
    assert draft(client, trace_id="tr3").status_code == 409
    assert draft(client, session_id="foreign").status_code == 404
    assert draft(client, trace_id="missing").status_code == 404
    assert draft(client, trace_id="").status_code == 422


def test_concurrent_saves_do_not_lose_cases(env):
    client, _, root = env

    def save(i):
        return client.post(
            "/api/evals/cases", json={"case": {**case(), "id": str(i), "expected_tools": []}}
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(save, range(8))) == [201] * 8
    assert len(load_instance_cases(root / "instance/config/evals/cases.json")) == 8


def test_malformed_existing_suite_is_preserved(env):
    client, _, root = env
    path = root / "instance/config/evals/cases.json"
    path.parent.mkdir(parents=True)
    path.write_text("broken")
    assert client.post("/api/evals/cases", json={"case": {**case(), "expected_tools": []}}).status_code == 409
    assert path.read_text() == "broken"


@pytest.mark.parametrize("endpoint", ["/api/evals/cases/draft", "/api/evals/cases"])
def test_operator_auth_required(monkeypatch, endpoint):
    from a2a_impl import auth

    monkeypatch.setattr(auth, "_BEARER", ["operator-secret"])
    monkeypatch.setattr(auth, "_FEDERATION", ["federation-secret"])
    app = FastAPI()
    register_eval_case_routes(app)
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        assert client.post(endpoint, json={}).status_code == 401
        assert client.post(endpoint, json={}, headers={"Authorization": "Bearer federation-secret"}).status_code == 403
        assert client.post(endpoint, json={}, headers={"Authorization": "Bearer operator-secret"}).status_code == 422


def test_failed_atomic_replace_preserves_existing_suite(env, monkeypatch):
    from infra import paths

    client, _, root = env
    value = {**case(), "expected_tools": ["search"]}
    assert client.post("/api/evals/cases", json={"case": value}).status_code == 201
    path = root / "instance/config/evals/cases.json"
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(paths.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        client.post("/api/evals/cases", json={"case": {**value, "id": "second"}})
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".cases.json.*"))
