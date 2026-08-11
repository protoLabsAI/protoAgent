"""The suite must not write into a LIVE agent's stores, whoever spawned it (#2543).

Running `python -m pytest tests/ -q` from inside a live agent — a board gate, a coder
verifying a feature in a worktree — hands pytest that agent's environment. On
2026-08-10 that put 17 test-fixture sessions (`hold-2`..`hold-6` "deploy the service",
`g1`/`g2`/`h1` goal kickoffs, `sess-BB`/`sess-BG`, `s1`/`iso1`/`multi-1`…) into a running
fleet member's session store, where its operator saw them as a flood of junk chats. The
same fixture ids had already landed there on 2026-07-24, so it reproduced on every
full-suite run spawned that way.

`tests/conftest.py::_isolate_instance_roots` is the guard. This is the tripwire that
proves it, the only way it can be proven: by actually being the hostile spawner. An
in-process test can't — `monkeypatch.setenv` inside a test runs AFTER the autouse
fixture and would simply win, which is the fixture's designed escape hatch, not a
breach. So this shells out with a live agent's env exported and checks that the agent's
roots are untouched afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The files whose fixtures actually produced the observed junk sessions. A representative
# subset, not the whole suite: the guard is global (one autouse fixture), so these
# exercise it, and a nested full-suite run would cost two minutes per test run.
_POLLUTERS = (
    "tests/test_hitl_hold.py",
    "tests/test_goal_kickoff_live.py",
    "tests/test_background.py",
)

# Env the parent conftest neutralises per test. The child must NOT inherit them, or it
# would be isolated by the parent's fixtures rather than by its own — testing nothing.
_PARENT_ONLY = ("PROTOAGENT_HOST_CONFIG", "PROTOAGENT_INJECTION_LOG", "PROTOAGENT_PROMPT_SNAPSHOTS")


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.exists() else set()


def test_a_hostile_spawner_env_cannot_reach_the_live_stores(tmp_path):
    """Export a live agent's instance env, run the suite's worst offenders, and check
    that not one byte landed in that agent's roots."""
    live_instance = tmp_path / "live-agent"
    live_box = tmp_path / "live-box"
    (live_instance / "config").mkdir(parents=True)  # looks like a real instance root
    (live_box / "commons").mkdir(parents=True)
    before_instance, before_box = _tree(live_instance), _tree(live_box)

    env = {k: v for k, v in os.environ.items() if k not in _PARENT_ONLY}
    env.update(
        {
            # PROTOAGENT_HOME is TERMINAL — the dir it names IS the instance root, which
            # is exactly why an inherited one is so destructive.
            "PROTOAGENT_HOME": str(live_instance),
            "PROTOAGENT_BOX_ROOT": str(live_box),
            "PROTOAGENT_INSTANCE": "prod",
        }
    )

    run = subprocess.run(
        [sys.executable, "-m", "pytest", *_POLLUTERS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert run.returncode == 0, f"the child run itself failed:\n{run.stdout[-3000:]}"

    assert _tree(live_instance) == before_instance, (
        "the suite wrote into the spawning agent's INSTANCE root — this is the #2543 "
        f"defect: {sorted(_tree(live_instance) - before_instance)}"
    )
    assert _tree(live_box) == before_box, (
        f"the suite wrote into the spawning agent's BOX root: {sorted(_tree(live_box) - before_box)}"
    )


def test_resolution_never_lands_on_the_developers_real_home():
    """The fallback half of the guard. With the ambient vars cleared, resolution would
    otherwise settle on `data_home()` — a developer's real `~/.protoagent`, or
    `~/Library/Application Support/...` on a desktop box."""
    from infra.paths import instance_paths

    p = instance_paths()
    real_homes = {Path.home() / ".protoagent", Path.home() / "Library" / "Application Support"}
    for root in (p.box_root, p.instance_root):
        assert not any(root == h or h in root.parents for h in real_homes), root
