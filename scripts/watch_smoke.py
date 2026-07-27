#!/usr/bin/env python3
"""Live smoke for the watch subsystem (ADR 0067) — boots throwaway instances and
drives every watch disposition over the real HTTP surface.

Sibling of ``live_smoke.py``: same "boot the real server, exercise the actual wire"
premise, aimed at the piece unit tests structurally cannot reach — the out-of-band
poller. ``WatchController`` is unit-tested thoroughly by calling ``tick_all``
directly; what that can never show is whether the SERVER starts the loop, honors
``watches.interval``, and reacts on the cadence an operator configured. #2320 was
exactly that shape: every controller test passed while no instance polled anything.

Costs zero model turns and needs no gateway. Every watch here is ``run_prompt``-less,
so ``_react`` never reaches ``run_in_session``, and predicates are files this script
owns rather than LLM judgments — the assertions are exact, not fuzzy.

Instance A runs with ``goal.enabled: false`` on purpose: the whole battery doubles as
proof that watches are decoupled from goal mode. Instance B exists only to make
retention observable in seconds instead of a day.

Exit 0 on success, non-zero (with the failing assertions named) otherwise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "watch-smoke-local"
# Fast enough to finish in CI, at/above MIN_WATCH_INTERVAL_S so the server doesn't
# floor it out from under the timing assertions.
INTERVAL_S = 5.0

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)
    if not ok:
        failures.append(f"{name} — {detail}" if detail else name)
    return ok


# --- instance plumbing ------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthz(port: int, timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — still booting
            pass
        time.sleep(1.0)
    return False


def boot(keep_terminal_h: float, *, goal_enabled: bool = False) -> tuple[subprocess.Popen, int, Path]:
    """Start a throwaway server. PROTOAGENT_HOME is terminal — the dir IS the
    instance root, so config lands at <home>/config/langgraph-config.yaml."""
    port = _free_port()
    home = Path(tempfile.mkdtemp(prefix="watch-smoke-"))
    # Isolate the BOX tier too, not just the instance: host-config.yaml is inherited by
    # every instance on the machine, so without this the smoke picks up the developer's
    # Host cascade layer (e.g. a widened `network.bind`) and stops being hermetic.
    box = Path(tempfile.mkdtemp(prefix="watch-smoke-box-"))
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "langgraph-config.yaml").write_text(
        "model:\n"
        "  name: protolabs/reasoning\n"
        # Deliberately unroutable: nothing in this battery may reach a model, and a
        # connection error is a louder failure than a silent bill.
        "  api_base: http://127.0.0.1:9/v1\n"
        "middleware:\n  knowledge: false\n  scheduler: false\n"
        f"goal:\n  enabled: {str(goal_enabled).lower()}\n"
        f"watches:\n  enabled: true\n  interval: {INTERVAL_S}\n  keep_terminal_h: {keep_terminal_h}\n"
        f"auth:\n  token: {TOKEN}\n"
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "fake-watch-smoke-key",
        "PROTOAGENT_HOME": str(home),
        "PROTOAGENT_BOX_ROOT": str(box),
        "PROTOAGENT_INSTANCE": "watchsmoke",
        "PROTOAGENT_HEADLESS_SETUP": "1",
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "server", "--ui", "none", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, port, home


# --- HTTP -------------------------------------------------------------------


def api(port: int, method: str, path: str, body: dict | None = None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode()
            return r.status, (text if raw else json.loads(text or "{}"))
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text or "{}")
        except json.JSONDecodeError:
            return e.code, {"detail": text}


def watches(port: int) -> dict[str, dict]:
    _, b = api(port, "GET", "/api/watches")
    return {w["id"]: w for w in b.get("watches", [])}


def create(port: int, wid: str, verifier: dict, **kw) -> tuple[int, dict]:
    return api(
        port,
        "POST",
        "/api/watches",
        {"watch_id": wid, "condition": kw.pop("condition", f"{wid} probe"), "verifier": verifier, **kw},
    )


def wait_checks(port: int, wid: str, n: int, timeout: float = 0.0) -> dict | None:
    """Block until the watch has been evaluated at least ``n`` times, went terminal,
    or vanished. Synchronizing on ``check_count`` rather than sleeping is what keeps
    the battery correct no matter where in the tick it starts — and it lets every
    case run concurrently, since one ``tick_all`` advances all of them."""
    end = time.time() + (timeout or INTERVAL_S * 12)
    while time.time() < end:
        w = watches(port).get(wid)
        if w is None or w["check_count"] >= n or w["status"] != "active":
            return w
        time.sleep(0.5)
    return watches(port).get(wid)


# --- the battery ------------------------------------------------------------


def battery(port: int, work: Path) -> None:
    print("\n== surfaces ==", flush=True)
    _, cat = api(port, "GET", "/api/verifiers")
    types = {t["value"]: t for t in cat.get("types", [])}
    check("the verifier catalog serves the core types, `plugin` included", "plugin" in types, f"{sorted(types)}")
    check("every catalog entry is source-labelled", bool(types) and all(t.get("source") for t in types.values()))

    cfg = api(port, "GET", "/api/config")[1]["config"]
    check(
        "watches.{enabled,interval,keep_terminal_h} round-trip config",
        {"enabled", "interval", "keep_terminal_h"} <= set(cfg.get("watches") or {}),
        f"{cfg.get('watches')}",
    )
    goal_on = (cfg.get("goal") or {}).get("enabled")
    check("this instance really has goal mode OFF (the #2320 premise)", goal_on is False, f"goal.enabled={goal_on}")

    _, mtext = api(port, "GET", "/metrics", raw=True)
    check("both watch counters are exported", "protoagent_watch_fires_total" in mtext and "protoagent_watch_flapping_total" in mtext)

    print("\n== create + validation ==", flush=True)
    never = str(work / "never.txt")
    Path(never).write_text("nothing\n")
    st, b = create(port, "v-ok", {"type": "data", "path": never, "contains": "NOPE"})
    check("the operator channel accepts a non-plugin verifier", st == 200 and b.get("ok"))
    st, b = create(port, "v-bad", {"type": "data", "path": never, "contains": "x"}, trigger="whenever")
    check("an unknown trigger is rejected, not silently defaulted", st == 400 and "unknown trigger" in json.dumps(b).lower(), f"{st}")
    st, _ = api(port, "POST", "/api/watches", {"condition": "", "verifier": {"type": "llm"}})
    check("a blank condition is rejected", st == 400, f"{st}")
    st, b = api(port, "PATCH", "/api/watches/v-ok", {"verifier": {}})
    check("an empty verifier is not a way to disarm a watch", st == 400 and "clear the watch" in json.dumps(b).lower(), f"{st}")

    print("\n== the poller runs with goal mode off (#2320) ==", flush=True)
    w = wait_checks(port, "v-ok", 2)
    check(
        "a watch is POLLED on a goal-mode-off instance",
        bool(w and w["check_count"] >= 2),
        f"check_count={w['check_count'] if w else 'missing'} (the shipped bug left this at 0 forever)",
    )
    check("it is genuinely evaluating, not merely listed active", bool(w and w["last_checked"] and w["last_reason"]), f"last_reason={w['last_reason']!r}" if w else "")

    print("\n== lifecycle ==", flush=True)
    one, edge, mon, frozen = (str(work / n) for n in ("one.txt", "edge.txt", "mon.txt", "frozen.txt"))
    for p in (one, edge):
        Path(p).write_text("waiting\n")
    Path(mon).write_text("v1\n")
    Path(frozen).write_text("frozen\n")

    create(port, "one-shot", {"type": "data", "path": one, "contains": "READY"})
    create(port, "edge", {"type": "data", "path": edge, "contains": "READY"}, repeat=True)
    create(port, "monitor", {"type": "data", "path": mon, "contains": "never"}, trigger="change", repeat=True)
    create(port, "expiry", {"type": "data", "path": one, "contains": "IMPOSSIBLE"}, deadline=time.time() + INTERVAL_S * 2)
    create(port, "stall", {"type": "data", "path": frozen, "contains": "never"}, stall_after=2)
    create(port, "slow", {"type": "data", "path": frozen, "contains": "never"}, interval_s=3600)
    wait_checks(port, "one-shot", 1)

    Path(one).write_text("READY\n")
    w = wait_checks(port, "one-shot", 99)
    check("a one-shot tripwire finishes `met` and records the fire", bool(w and w["status"] == "met" and w["fire_count"] == 1), f"status={w['status'] if w else '-'}")
    frozen_at = w["check_count"] if w else 0

    check("a change monitor's FIRST check only establishes a baseline", watches(port)["monitor"]["fire_count"] == 0)

    Path(edge).write_text("READY\n")
    w = wait_checks(port, "edge", watches(port)["edge"]["check_count"] + 1)
    check("a repeating watch fires on the rising edge and stays active", w["fire_count"] == 1 and w["status"] == "active", f"fires={w['fire_count']}")
    w = wait_checks(port, "edge", w["check_count"] + 3, timeout=INTERVAL_S * 14)
    check(
        "a LATCHED predicate does not re-fire every tick (edge, not level)",
        w["fire_count"] == 1,
        f"checks={w['check_count']} fires={w['fire_count']} (must stay 1 — each fire can enqueue a turn)",
    )
    Path(edge).write_text("waiting\n")
    w = wait_checks(port, "edge", w["check_count"] + 1)
    Path(edge).write_text("READY\n")
    w = wait_checks(port, "edge", w["check_count"] + 1)
    check("the next rising edge fires again", w["fire_count"] == 2, f"fires={w['fire_count']}")

    w = watches(port).get("one-shot")
    check("a terminal watch is no longer polled", w is None or w["check_count"] == frozen_at, f"{frozen_at} -> {w['check_count'] if w else 'pruned'}")

    base = watches(port)["monitor"]["check_count"]
    Path(mon).write_text("v2\n")
    w = wait_checks(port, "monitor", base + 1)
    check("moving the evidence fires the monitor", w["fire_count"] == 1, f"fires={w['fire_count']}")
    w = wait_checks(port, "monitor", w["check_count"] + 2, timeout=INTERVAL_S * 10)
    check("a static value does not fire the monitor", w["fire_count"] == 1, f"checks={w['check_count']} fires={w['fire_count']}")

    w = watches(port).get("expiry")
    check("a deadline that passes unmet finishes `expired`", bool(w and w["status"] == "expired"), f"status={w['status'] if w else 'gone'}")

    w = wait_checks(port, "stall", 3, timeout=INTERVAL_S * 10)
    check("unchanged evidence trips the stall hook and the watch stays active", w["stalled_notified"] and w["status"] == "active", f"streak={w['stall_streak']}")

    slow, fast = watches(port)["slow"], watches(port)["stall"]
    check(
        "a per-watch interval_s throttles below the global tick (#1753)",
        slow["check_count"] == 1 and fast["check_count"] > slow["check_count"],
        f"interval_s=3600 -> {slow['check_count']} vs default -> {fast['check_count']}",
    )

    print("\n== edit in place (#2328) ==", flush=True)
    before = watches(port)["stall"]
    st, _ = api(port, "PATCH", "/api/watches/stall", {"condition": "edited live", "interval_s": 45})
    after = watches(port)["stall"]
    check(
        "an edit preserves the id and accrued counters (not clear-and-recreate)",
        st == 200 and after["check_count"] == before["check_count"] and after["stall_streak"] == before["stall_streak"],
        f"checks {before['check_count']}->{after['check_count']} streak {before['stall_streak']}->{after['stall_streak']}",
    )
    check("the edited fields did change", after["condition"] == "edited live" and after["interval_s"] == 45)
    api(port, "PATCH", "/api/watches/slow", {"deadline": time.time() + 9999})
    api(port, "PATCH", "/api/watches/slow", {"deadline": None})
    check("an explicit null clears a field (PATCH, not PUT)", watches(port)["slow"]["deadline"] is None)
    st, _ = api(port, "PATCH", "/api/watches/nope", {"condition": "x"})
    check("editing a missing watch 400s rather than creating one", st == 400, f"{st}")

    print("\n== flap instrumentation ==", flush=True)
    # `date +%s` — NOT %N: BSD date emits "%N" literally, so the flapper would
    # silently never flap and the assertion would pass for the wrong reason.
    create(port, "flapper", {"type": "command", "command": "date +%s"}, trigger="change", repeat=True, condition="a value that moves every check")
    w = wait_checks(port, "flapper", 7, timeout=INTERVAL_S * 16)
    check("a value that moves every check produces consecutive fires", w["consecutive_fires"] >= 5, f"consecutive={w['consecutive_fires']}")
    check("the flap warning latched once for the episode", w["flap_warned"])
    _, mtext = api(port, "GET", "/metrics", raw=True)
    fired = [ln for ln in mtext.splitlines() if ln.startswith("protoagent_watch_fires_total{")]
    check("the fires counter is labelled by trigger only (no unbounded watch id)", bool(fired) and not any("id=" in ln for ln in fired), f"{fired[:2]}")

    print("\n== teardown ==", flush=True)
    for wid in list(watches(port)):
        api(port, "DELETE", f"/api/watches/{wid}")
    _, b = api(port, "DELETE", "/api/watches/one-shot")
    check("a repeat DELETE is idempotent", b.get("cleared") is False)
    check("every watch is gone", not watches(port), f"{sorted(watches(port))}")


def retention(port: int, work: Path, keep_h: float) -> None:
    print(f"\n== retention (keep_terminal_h={keep_h}h = {keep_h * 3600:.1f}s) ==", flush=True)
    hit = str(work / "hit.txt")
    Path(hit).write_text("READY\n")
    create(port, "terminal", {"type": "data", "path": hit, "contains": "READY"})
    w = wait_checks(port, "terminal", 99)
    check("the watch reached a terminal state", bool(w and w["status"] == "met"), f"status={w['status'] if w else 'gone'}")

    end = time.time() + max(30.0, keep_h * 3600 * 4)
    pruned = False
    while time.time() < end:
        if "terminal" not in watches(port):
            pruned = True
            break
        time.sleep(0.5)
    check("the TICK retires it once it ages past keep_terminal_h", pruned, "" if pruned else "still listed — retention is not running on the tick")


def main() -> int:
    started = time.time()
    work = Path(tempfile.mkdtemp(prefix="watch-smoke-work-"))

    # Instance A — goal mode OFF on purpose, so the whole battery doubles as live
    # proof of the decoupling. Retention long enough that terminal watches persist
    # for the assertions that read them.
    proc, port, _home = boot(keep_terminal_h=24.0)
    try:
        if not _wait_healthz(port):
            print("FAIL: /healthz never returned 200 (server did not become ready)")
            return 1
        print(f"ok: throwaway booted on :{port} (goal off, interval {INTERVAL_S:g}s)")
        battery(port, work)
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    # Instance B — retention only. Separate because the value that makes pruning
    # observable in seconds would delete the terminal watches instance A asserts on.
    keep_h = 0.002  # 7.2s
    proc, port, _home = boot(keep_terminal_h=keep_h)
    try:
        if not _wait_healthz(port):
            print("FAIL: retention instance did not become ready")
            return 1
        retention(port, work, keep_h)
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    print(f"\n{'=' * 62}")
    if failures:
        print(f"{len(failures)} FAILED ({time.time() - started:.0f}s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"watch smoke PASSED ({time.time() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
