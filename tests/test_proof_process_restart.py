"""Actual SIGKILL proof: boots two independent interpreters per scenario; cold startup can exceed five seconds."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import pytest

pytestmark = [pytest.mark.platform_sensitive,
              pytest.mark.skipif(os.name != "posix", reason="requires POSIX SIGKILL semantics")]
ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(scope="module", params=[(point, caller) for point in ("accepted", "claimed", "parked")
    for caller in ("federation", "fleet-service", "paired-agent", "enrolled-console")],
    ids=lambda p: "-".join(p))
def proof(request, tmp_path_factory):
    point, caller = request.param
    base = tmp_path_factory.mktemp(f"restart-{point}-{caller}")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
           "PROTOAGENT_HOME": str(base / "home"), "PROTOAGENT_BOX_ROOT": str(base / "box")}
    cmd = [sys.executable, "-m", "tests._proof_restart_child", str(base), point, caller]
    with (base / "child.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen([*cmd, "prepare"], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while not (base / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (base / "ready").exists(), (base / "child.log").read_text()
            assert child.poll() is None
            child.send_signal(signal.SIGKILL)
            assert child.wait(timeout=10) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    second = subprocess.run([*cmd, "recover"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
    assert second.returncode == 0, second.stdout + second.stderr
    report = json.loads((base / "report.json").read_text())
    assert report["pid"] != child.pid
    return point, caller, base, report

def test_killed_process_preserves_partition_and_reservation(proof):
    point, caller, base, report = proof
    rec = report["recovery"]
    assert rec == report["repeated"]
    turns = sorted(rec["queued"] + rec["suspended"] + rec["parked"], key=lambda t: t["seq"])
    assert [(t["seq"], t["dedupe"], t["audience"]) for t in turns] == [
        (1, "private", "private"), (2, "peer-1", "peer"), (3, "peer-2", "peer")]
    assert all(t["checkpoint"] == report["checkpoint"] == "proj:atlas:restart-proof" for t in turns)
    assert [t["principal"] for t in turns] == ["device:enrolled-console", f"device:{caller}", f"device:{caller}"]
    assert turns[0]["input_ref"] == json.loads((base / "ids.json").read_text())["ref"]
    assert report["transcript"]["input"] == "PRIVATE-RESTART-CANARY"
    assert report["transcript"]["reply"] == "PRIVATE-REPLY-CANARY"
    assert report["visible"] == (report["transcript"] if caller == "enrolled-console" else None)
    assert "public receipt" in report["peer_task"] and "CANARY" not in report["peer_task"]
    assert not report["public"] and report["unresolved"] is None and report["sends"] == []
    assert report["denied"] == (caller != "enrolled-console")
    # P1a "no operator-tier shortcut": the fleet token and an agent-kind device are operator TIER yet
    # non-operator principals, so their rejection is a principal-kind restriction, not a tier free pass;
    # only a device-kind pairing reads as the human operator.
    assert report["principal_kind"] == {"federation": "federation", "fleet-service": "fleet-service",
        "paired-agent": "agent-device", "enrolled-console": "device"}[caller]
    assert report["tier"] == ("federation" if caller == "federation" else "operator")
    assert report["denied"] == (report["principal_kind"] not in ("shared-operator-bearer", "device"))
    assert rec["audience"] == "private" and rec["reserved"] == (point != "accepted")
    effects = (base / "effects").read_text().splitlines() if (base / "effects").exists() else []
    assert effects == (["effect"] if point == "claimed" else [])
    sends = (base / "sends").read_text().splitlines() if (base / "sends").exists() else []
    assert sends == ([turns[0]["input_ref"]] if point != "accepted" else [])
    assert report["next"] == (turns[0] if point == "accepted" else None)
    if point != "accepted":
        assert rec["segment"]["state"] == ("running" if point == "claimed" else "parked")
        assert [t["seq"] for t in rec["suspended" if point == "claimed" else "parked"]] == [1]
    if point == "parked":
        assert report["interrupts"] == [rec["segment"]["interrupt_id"]]
        assert report["wrong_resume"] is None
        assert report["matching_resume"] == rec["segment"]["generation"] + 1

@pytest.mark.xfail(strict=True, reason="PROOF FAILURE: resolver metadata is memory-only and lost on SIGKILL restart")
def test_original_resolver_metadata_survives_process_restart(proof):
    report = proof[3]
    assert (report["metadata"], report["resolved"]) == ({"project": "atlas"}, "proj:atlas:restart-proof")
