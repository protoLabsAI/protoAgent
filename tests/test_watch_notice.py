"""Watch fires reach the owner once, without making delivery a tick dependency."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from graph.config import LangGraphConfig
from graph.goals.verifiers import VerifyResult
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore
from graph.watches.types import FLAP_WARN_CONSECUTIVE_FIRES
from runtime.state import STATE
from scheduler.local import LocalScheduler

pytestmark = [pytest.mark.asyncio, pytest.mark.platform_sensitive]


@pytest.fixture
def notices(tmp_path, monkeypatch):
    notify = Mock()
    monkeypatch.setattr("graph.plugins.chat_surface.notify_chat_surfaces", notify)
    monkeypatch.setattr("graph.sdk.console_url", lambda: "https://console.example")
    monkeypatch.setattr("security.public_console.public_origin", lambda: "")
    monkeypatch.setattr(
        STATE, "scheduler",
        LocalScheduler("watch-notice", invoke_url="http://test", db_dir=tmp_path / "scheduler"),
    )
    return notify


def controller(tmp_path, monkeypatch, **options):
    (tmp_path / "watches").mkdir()
    ctrl = WatchController(LangGraphConfig(), WatchStore(tmp_path / "watches"))
    ctrl.create(
        condition="deploy ready", watch_id="deploy",
        verifier={"type": "plugin", "check": "test:ready"}, **options,
    )
    monkeypatch.setattr(
        "graph.watches.controller.run_verifier",
        AsyncMock(return_value=VerifyResult(True, "healthy", "ready")),
    )
    return ctrl


@pytest.mark.parametrize("session", ["", "chat-7"])
async def test_met_watch_notifies_once_with_console_link(tmp_path, monkeypatch, notices, session):
    ctrl = controller(tmp_path, monkeypatch, run_session=session)
    assert await ctrl.evaluate("deploy") == "met"
    await ctrl.evaluate("deploy")
    notices.assert_called_once_with(
        "Watch deploy met: healthy",
        "https://console.example/app/" + ("?session=chat-7" if session else ""),
        kind="notify", importance="normal", source="watch",
    )


async def test_change_notices_stop_while_flapping_and_resume_after_settling(tmp_path, monkeypatch, notices):
    ctrl = controller(tmp_path, monkeypatch, trigger="change", repeat=True)
    results = [VerifyResult(False, "moved", str(i)) for i in range(FLAP_WARN_CONSECUTIVE_FIRES + 2)]
    monkeypatch.setattr("graph.watches.controller.run_verifier", AsyncMock(side_effect=results))
    await ctrl.evaluate("deploy")  # baseline
    notices.assert_not_called()
    for _ in range(FLAP_WARN_CONSECUTIVE_FIRES - 1):
        await ctrl.evaluate("deploy")
    assert notices.call_count == FLAP_WARN_CONSECUTIVE_FIRES - 1
    assert notices.call_args.args[0] == "Watch deploy changed: moved"
    notices.reset_mock()
    await ctrl.evaluate("deploy")
    await ctrl.evaluate("deploy")
    notices.assert_not_called()
    monkeypatch.setattr(
        "graph.watches.controller.run_verifier",
        AsyncMock(side_effect=[results[-1], VerifyResult(False, "settled change", "new")]),
    )
    await ctrl.evaluate("deploy")  # unchanged resets the flap streak
    await ctrl.evaluate("deploy")
    notices.assert_called_once()


@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_reaction_completes_without_a_second_notice(tmp_path, monkeypatch, notices, caplog, delivery_fails):
    from server import a2a

    if delivery_fails:
        notices.side_effect = RuntimeError("delivery unavailable")
    ctrl = controller(tmp_path, monkeypatch, run_session="chat-7", run_prompt="Run smoke tests")
    hook = AsyncMock()
    monkeypatch.setattr("graph.watches.hooks.fire_watch_hook", hook)
    assert await ctrl.tick_all() == 1
    watch = ctrl.store.get("deploy")
    assert watch.status == "met" and not watch.reaction_pending
    hook.assert_awaited_once()
    job = STATE.scheduler.get_job("watch-deploy")
    assert job.context_id == "chat-7" and "Run smoke tests" in job.prompt
    # Exercise the actual scheduled completion routing with the job created by SDK.
    deliver = AsyncMock()
    monkeypatch.setattr(a2a, "_deliver_scheduled", deliver)
    a2a._handle_scheduled_delivery(SimpleNamespace(
        origin="scheduler", trigger=job.id, context_id=job.context_id,
        text="Smoke tests passed", state="completed",
    ))
    deliver.assert_not_called()
    await ctrl.tick_all()
    notices.assert_called_once()
    assert notices.call_args.kwargs == {"kind": "notify", "importance": "normal", "source": "watch"}
    if delivery_fails:
        assert "could not queue the notice for deploy" in caplog.text
