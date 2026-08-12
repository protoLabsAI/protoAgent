"""Ensure deterministic import resolution for the protoagent test suite.

Moves site-packages to the front of sys.path so installed packages
(langchain_core, langchain, etc.) are never shadowed by local directories
that pytest inserts during collection.
"""

from __future__ import annotations

import os
import site
import sys

import pytest


# Every env var that can steer instance-path resolution. Cleared per test — see
# `_isolate_instance_roots`.
_INSTANCE_ROOT_ENV = (
    "PROTOAGENT_HOME",
    "PROTOAGENT_INSTANCE",
    "PROTOAGENT_BOX_ROOT",
    "PROTOAGENT_AUTO_SCOPE",
    "PROTOAGENT_WORKSPACE",
    "PROTOAGENT_PLUGINS_DIR",
    "PROTOAGENT_PLUGINS_LOCK",
)


@pytest.fixture(autouse=True)
def _isolate_instance_roots(tmp_path, monkeypatch):
    """Make the suite hermetic against AMBIENT instance env, whoever spawned it (#2543).

    Running the suite from inside a live agent — a board gate, a coder verifying a
    feature in a worktree — hands pytest that agent's environment. Store fixtures then
    resolved their target from it and read and WROTE the live agent's data: 17 test
    sessions (``hold-2``..``hold-6``, ``g1``, ``sess-BB``, ``s1``, ``iso1``…) appeared in
    a running fleet member's session store on 2026-08-10, visible to its operator as a
    flood of junk chats, and the same fixture ids had already landed there on 07-24. The
    store-pollution sibling of the ADR-0096 incident where a coder's test run deleted
    live data.

    Two things have to be true, and neither alone is enough:

    * the inherited vars are cleared — ``PROTOAGENT_HOME`` is TERMINAL (the dir it names
      IS the instance root), so an exported one points every store straight at the live
      instance no matter what the fallback says; and
    * the fallback is pinned — with the vars gone, resolution lands on ``data_home()``,
      i.e. the developer's real ``~/.protoagent``.

    A test that wants its own roots still wins: its ``monkeypatch`` runs after this one.
    That is how the ~41 tests that set these vars, and the handful that patch
    ``data_home`` directly, keep working unchanged."""
    from infra import paths as _paths

    for var in _INSTANCE_ROOT_ENV:
        monkeypatch.delenv(var, raising=False)
    box = tmp_path / "box-root"
    monkeypatch.setattr(_paths, "data_home", lambda: box)


@pytest.fixture(autouse=True)
def _reset_instance_paths(_isolate_instance_roots):
    """Re-resolve ``infra.paths.instance_paths()`` cleanly for every test.

    The frozen ``InstancePaths`` singleton is resolved-once-and-cached from the
    environment (PROTOAGENT_HOME / PROTOAGENT_INSTANCE / PROTOAGENT_BOX_ROOT), so a
    test that sets one of those vars (or monkeypatches ``data_home``) needs the
    cache cleared or it'd read a stale path. Reset BEFORE (so a test's env is seen
    on the first ``instance_paths()`` call) and AFTER (so a stale cache never leaks
    into the next test).

    Depends on ``_isolate_instance_roots`` for ORDER, not data: the ambient env has to
    be gone before the first resolution, or the cache is seeded from the live agent."""
    from infra.paths import reset_instance_paths

    reset_instance_paths()
    yield
    reset_instance_paths()


@pytest.fixture(autouse=True)
def _isolate_injection_log(tmp_path, monkeypatch):
    """Point the per-turn injection log (ADR 0069 D6) at a per-test temp DB.

    ``KnowledgeMiddleware.before_model`` appends an injection row whenever it
    injects memory — which many unrelated tests drive — so without this every
    local/CI test run would write rows into the developer's REAL instance
    store. Lazy: only tests that actually trigger a record create the DB."""
    from observability.injection_log import reset_injection_log

    monkeypatch.setenv("PROTOAGENT_INJECTION_LOG", str(tmp_path / "injection-log.db"))
    reset_injection_log()
    yield
    reset_injection_log()


@pytest.fixture(autouse=True)
def _isolate_prompt_snapshots(tmp_path, monkeypatch):
    """Point the prompt snapshot store (#2243) at a per-test temp DB.

    ``PromptCaptureMiddleware`` records a snapshot on every wrapped model call
    and capture is default-ON — so, like the injection log above, any test that
    drives the middleware stack would otherwise write into the developer's REAL
    instance store. Lazy: only tests that actually record create the DB."""
    from observability.prompt_snapshots import reset_prompt_snapshots

    monkeypatch.setenv("PROTOAGENT_PROMPT_SNAPSHOTS", str(tmp_path / "prompt-snapshots.db"))
    reset_prompt_snapshots()
    yield
    reset_prompt_snapshots()


@pytest.fixture(autouse=True)
def _isolate_host_config(tmp_path, monkeypatch):
    """Point the ADR-0047 Host layer at a per-test ABSENT tmp file.

    The session-level setdefault in pytest_configure was enough while nothing ever
    WROTE host-config.yaml — but #2533's host model-layer mirror writes it at the
    reload commit, and the old '/nonexistent/…' sentinel is only unwritable on
    POSIX: on Windows it resolves to a creatable path under the drive root, so one
    test's mirror write polluted every later cascade read (the v0.132.0 release-PR
    Windows failure). Per-test tmp keeps mirror writes real AND isolated; a test
    that wants its own layer still wins with monkeypatch.setenv."""
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(tmp_path / "host-config.yaml"))


def pytest_configure(config):  # noqa: ARG001
    """Prepend site-packages to sys.path before any test imports occur."""
    site_dirs = site.getsitepackages()
    for sp in reversed(site_dirs):
        if sp in sys.path:
            sys.path.remove(sp)
        sys.path.insert(0, sp)

    # Default-on context compaction builds a summarizer LLM whenever the
    # middleware stack is assembled, and ChatOpenAI requires a key at
    # construction. Production always has one at graph-build time; provide a
    # dummy so middleware-wiring tests don't each need to set it.
    # `setdefault` never overrides a real key, and no test asserts key-absence.
    os.environ.setdefault("OPENAI_API_KEY", "test-key")

    # Isolate the ADR-0047 Host layer for IMPORT-TIME reads only — the autouse
    # `_isolate_host_config` fixture owns per-test isolation (a per-test tmp path,
    # writable so the #2533 mirror works but never shared between tests). This
    # setdefault just keeps any pre-fixture read off the dev/CI machine's real file.
    os.environ.setdefault("PROTOAGENT_HOST_CONFIG", "/nonexistent/protoagent-host-config.test.yaml")


@pytest.fixture(autouse=True)
def _reset_a2a_auth_guard():
    """Reset the A2A auth guard between tests — it is process-global by design.

    ``a2a_impl.auth`` holds the active credentials in module-level slots so a wizard/drawer
    reload can rotate them live without re-registering routes. That makes them shared state
    across a pytest session: a suite that configures a bearer and returns leaves it set for
    everything after it.

    It only started to bite once the self-invocation paths (scheduler, background manager)
    began resolving credentials from the guard at call time instead of capturing them at
    construction — which is what fixes the boot-order regression in #2620, and what makes
    a leaked bearer show up as a mystery Authorization header in an unrelated test. Reset
    after each test so the leak can't travel; tests that configure the guard are unaffected.
    """
    yield
    from a2a_impl import auth

    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._FLEET[0] = None
    auth._API_KEY[0] = ""
    auth._API_KEY_DEPRECATION_LOGGED[0] = False
