"""Exercise teardown across real pytest test boundaries, including loop shutdown."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.platform_sensitive
def test_private_steering_work_is_drained_before_the_next_test(tmp_path):
    # A subprocess is needed to observe fixture teardown after a test has returned.
    # Include the actual expected-runner leaker and both original idempotency tests.
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "isolation_probe.py").write_text(textwrap.dedent('''
        import sys
        import pytest
        from tests.conftest import _reset_recovered_private_turns

        def pytest_sessionstart(session):
            from types import SimpleNamespace
            from runtime.state import STATE
            # Model a worker whose app config enables private scheduling. The
            # legacy intake tests must select their own mode, regardless of it.
            STATE.graph_config = SimpleNamespace(private_steering_enabled=True)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_teardown(item):
            yield
            chat = sys.modules.get("server.chat")
            if chat is not None:
                assert not chat._STEER_RESTARTS, item.nodeid
                assert not chat._PRIVATE_RUNS, item.nodeid
                assert not chat._RECOVERED_PRIVATE_TURNS, item.nodeid
    '''), encoding="utf-8")
    case = tmp_path / "test_detached_recovery.py"
    case.write_text(textwrap.dedent('''
        import asyncio
        import importlib
        from types import SimpleNamespace
        from runtime.state import STATE
        from a2a_impl import segment_schedule

        chat = importlib.import_module("server.chat")
        finished = []

        async def test_leave_recovery_in_flight(monkeypatch):
            config = SimpleNamespace(private_steering_enabled=True)
            monkeypatch.setattr(STATE, "graph_config", config)
            monkeypatch.setattr(chat, "_private_partition_store", lambda: object())
            started = asyncio.Event()

            async def sessions(*args):
                return ["isolation"]

            async def recover(*args, **kwargs):
                return {"reserved": False, "suspended": [], "parked": [],
                        "queued": [{"audience": segment_schedule.AUDIENCE_PRIVATE}]}

            async def dispatch(*args):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)  # cancellation must be awaited, not just sent
                    assert STATE.graph_config is config
                    chat._expect_private_run("isolation", 1)
                    chat._RECOVERED_PRIVATE_TURNS["isolation"] = object()
                    finished.append(True)

            monkeypatch.setattr(segment_schedule, "sessions_with_pending_turns", sessions)
            monkeypatch.setattr(chat, "recover_durable_steers", recover)
            monkeypatch.setattr(chat, "_dispatch_recovered_private_turn", dispatch)
            result = await chat.recover_private_pending_turns()
            await started.wait()
            assert result["dispatch"] in chat._STEER_RESTARTS

        def test_next_test_sees_completed_cancellation():
            assert finished == [True]
    '''), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(root), str(root / "src")))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "isolation_probe",
         "-c", str(root / "pyproject.toml"),
         "tests/test_private_steering_parity.py::test_private_retry_and_concurrent_delivery[True]",
         str(case),
         "tests/test_steer_idle_starts_turn.py::test_private_steer_is_idempotent_across_a_retry",
         "tests/test_steer_idle_starts_turn.py::test_two_concurrent_private_steers_record_one_shell_and_start_one_turn"],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout
