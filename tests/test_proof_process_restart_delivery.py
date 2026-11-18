"""Two cold interpreters and SIGKILL per case; dependency startup can exceed five seconds."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from tests._proof_restart_child import append
from tests._proof_restart_delivery_child import RESULT, TASK

pytestmark = [pytest.mark.platform_sensitive,
              pytest.mark.skipif(os.name != "posix", reason="requires POSIX SIGKILL semantics")]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", params=[(point, caller) for point in ("committed", "sent")
    for caller in ("federation", "fleet-service", "paired-agent", "enrolled-console")],
    ids=lambda p: "-".join(p))
def proof(request, tmp_path_factory):
    point, caller = request.param
    base = tmp_path_factory.mktemp(f"delivery-{point}-{caller}")

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            append(base / "sink.jsonl", json.dumps({"destination": self.path, "body": body}))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
           "PROTOAGENT_HOME": str(base / "home"), "PROTOAGENT_BOX_ROOT": str(base / "box"),
           "PUSH_NOTIFICATION_ALLOWED_HOSTS": "127.0.0.1"}
    cmd = [sys.executable, "-m", "tests._proof_restart_delivery_child", str(base), point, caller]
    sink = f"http://127.0.0.1:{server.server_port}"
    try:
        with (base / "child.log").open("w") as log:
            child = subprocess.Popen([*cmd, "prepare", sink], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 45
                while not (base / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert (base / "ready").exists(), (base / "child.log").read_text()
                ready = json.loads((base / "ready").read_text())
                assert ready["pid"] == child.pid and child.poll() is None
                assert ready["record"]["state"] == ("prepared" if point == "committed" else "dispatching")
                before = (base / "sink.jsonl").read_text().splitlines() if (base / "sink.jsonl").exists() else []
                assert len(before) == (point == "sent")
                child.send_signal(signal.SIGKILL)
                assert child.wait(timeout=10) == -signal.SIGKILL
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
        second = subprocess.run([*cmd, "recover", sink], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=45)
        assert second.returncode == 0, second.stdout + second.stderr
        report = json.loads((base / "report.json").read_text())
        assert report["pid"] != child.pid
        assert report["before"] == ready["record"]
        received = [json.loads(line) for line in (base / "sink.jsonl").read_text().splitlines()]
        return point, caller, report, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_restart_delivers_only_sealed_payload_once_to_original_requester(proof):
    point, caller, report, received = proof
    assert len(received) == 1
    assert received[0]["destination"] == "/original"
    event = received[0]["body"]["artifactUpdate"]
    assert event["taskId"] == TASK
    assert event["artifact"]["parts"] == [{"text": RESULT}]
    assert "PRIVATE-" not in json.dumps(received)
    assert report["binding"] == report["original_binding"]
    assert report["binding"]["principal"] == f"device:{caller}"
    assert report["kind"] == {"federation": "federation", "fleet-service": "fleet-service",
                              "paired-agent": "agent-device", "enrolled-console": "device"}[caller]
    for key in ("id", "idempotency_key", "digest", "payload", "private_turn_id"):
        assert report["record"][key] == report["before"][key]
    assert report["record"]["payload"] == RESULT
    assert report["outcomes"] == ([True, False] if point == "committed" else [False, False])


def test_restart_delivery_record_is_terminal(proof):
    point, _, report, _ = proof
    # No field records a finished send except `delivered` itself, so a claim the dead
    # process left `dispatching` settles `closed`: once, with no replay, and for good.
    assert report["recovered"] == [point == "sent", False]
    assert report["settled"]["state"] == ("closed" if point == "sent" else "prepared")
    assert report["record"]["state"] == ("closed" if point == "sent" else "delivered")
