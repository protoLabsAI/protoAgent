"""Credential-bound paused attachments and the explicit owner handoff (#4274)."""

import asyncio
import hashlib
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from a2a_impl import auth, executor
from graph import sdk
from infra import media
from tests._a2a_app import build_app, drain_handlers, poll_terminal


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://agent.example")
    monkeypatch.setattr(media, "_PARKED", {})
    monkeypatch.setattr(auth, "_BEARER", ["owner-secret"])
    monkeypatch.setattr(auth, "_API_KEY", ["legacy-secret"])
    monkeypatch.setattr(auth, "_FEDERATION", ["federation-secret"])
    monkeypatch.setattr(auth, "_FLEET", ["fleet-secret"])
    monkeypatch.setattr(auth, "_ALLOWED_ORIGINS", [None])
    monkeypatch.setattr(
        auth,
        "_device_for_token",
        lambda token: (
            SimpleNamespace(id=token, effective_scope=lambda: "full") if token.startswith("device-") else None
        ),
    )


def context(kind):
    headers = (
        {"X-API-Key": "legacy-secret"}
        if kind in ("scheduler", "operator")
        else {"Authorization": f"Bearer {kind}-secret"}
    )
    return SimpleNamespace(call_context=SimpleNamespace(state={"headers": headers}))


@pytest.mark.parametrize("parked", ["scheduler", "operator", "owner", "federation", "device", "fleet", "unknown"])
@pytest.mark.parametrize(
    "answered", ["scheduler", "operator", "owner", "federation", "device", "device-other", "fleet", "unknown"]
)
def test_claim_policy_and_redacted_warning(parked, answered, caplog):
    before, after = context(parked), context(answered)
    p_kind = executor._principal_kind(before, "scheduler" if parked == "scheduler" else "")
    a_kind = executor._principal_kind(after, "scheduler" if answered == "scheduler" else "")
    assert p_kind == {"owner": "operator"}.get(parked, parked)
    assert a_kind == {"owner": "operator", "device-other": "device"}.get(answered, answered)
    p_id, a_id = executor._principal(before), executor._principal(after)
    attached = []
    token = executor._begin_turn_files()
    try:
        for content in (b"private attachment one", b"private attachment two"):
            ref = media.save_media(content, "image/png")
            attached.append(urlsplit(ref.url).path)
            assert sdk.attach_file(ref)
        executor._park_turn_files("paused-context", p_id, principal_kind=p_kind)
    finally:
        executor._end_turn_files(token)

    allowed = p_id == a_id or (parked in ("scheduler", "operator", "owner") and answered in ("operator", "owner"))
    with caplog.at_level(logging.WARNING, logger=executor.logger.name):
        token = executor._begin_turn_files("paused-context", a_id, principal_kind=a_kind, task_id="paused-task")
    try:
        files = executor._close_turn_files()
        assert len(files) == (2 if allowed else 0)
        assert all(f["url"].startswith("https://agent.example/media/") for f in files)
    finally:
        executor._end_turn_files(token)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == (0 if allowed else 1)
    if not allowed:
        message = warnings[0].getMessage()
        assert "2 paused-turn file(s)" in message
        assert "paused-context" in message and "paused-task" in message
        assert f"parked by {p_kind}, answered by {a_kind}" in message
        assert "withheld" in message and "dropped" not in message
        assert "files stay parked until TTL expiry/cancel or an authorized claim" in message
    for value in (*attached, "private attachment one", "private attachment two"):
        assert value not in caplog.text
    for secret in (
        "owner-secret",
        "legacy-secret",
        "federation-secret",
        "device-secret",
        "fleet-secret",
        "unknown-secret",
    ):
        assert secret not in caplog.text
        assert hashlib.sha256(secret.encode()).hexdigest() not in caplog.text
    assert p_id not in caplog.text and a_id not in caplog.text

    # Successful claims are consumed once; denied claims preserve the existing stash.
    token = executor._begin_turn_files("paused-context", p_id, principal_kind=p_kind)
    try:
        reclaimed = executor._close_turn_files()
        assert [urlsplit(f["url"]).path for f in reclaimed] == ([] if allowed else attached)
    finally:
        executor._end_turn_files(token)


@pytest.mark.parametrize("kind", ["federation", "device", "fleet", "unknown"])
def test_scheduler_origin_cannot_promote_another_credential(kind):
    assert executor._principal_kind(context(kind), "scheduler") == kind


def test_no_stash_no_warning(caplog):
    token = executor._begin_turn_files("absent", "other", principal_kind="operator", task_id="task")
    try:
        assert executor._close_turn_files() == []
        assert not caplog.records
    finally:
        executor._end_turn_files(token)


@pytest.mark.asyncio
async def test_scheduler_files_delivered_on_operator_resume(caplog):
    async def stream(text, ctx, *, resume=False, **kwargs):
        if resume:
            yield "done", "resumed"
        else:
            assert sdk.attach_file(media.save_media(b"scheduled report", "text/plain"), name="report.txt")
            yield "input_required", {"question": "Publish?"}

    app = build_app(stream, bearer="owner-secret", api_key="legacy-secret")

    async def send(client, headers, task=None):
        message = {"messageId": "answer" if task else "start", "role": "ROLE_USER", "parts": [{"text": "yes"}]}
        if task:
            message.update(taskId=task["id"], contextId=task["contextId"])
        return await client.post(
            "/a2a",
            headers={"A2A-Version": "1.0", **headers},
            json={
                "jsonrpc": "2.0",
                "id": "resume" if task else "park",
                "method": "SendMessage",
                "params": {"message": message, "metadata": {} if task else {"origin": "scheduler"}},
            },
        )

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await send(client, {"X-API-Key": "legacy-secret"})
            task = response.json()["result"]["task"]
            # Polling uses the operator bearer; only the answer claims the stash.
            client.headers["Authorization"] = "Bearer owner-secret"
            assert (await poll_terminal(client, task["id"]))["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
            assert (await send(client, {}, task)).status_code == 200
            for _ in range(60):
                final = await poll_terminal(client, task["id"])
                if final["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.03)
            assert final["status"]["state"] == "TASK_STATE_COMPLETED"
            files = [part for art in final["artifacts"] for part in art["parts"] if "url" in part]
            assert len(files) == 1
            assert files[0]["filename"] == "report.txt"
            assert "paused-turn file(s)" not in caplog.text
    finally:
        await drain_handlers()
