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


@pytest.fixture(autouse=True)
def _reset_instance_paths():
    """Re-resolve ``infra.paths.instance_paths()`` cleanly for every test.

    The frozen ``InstancePaths`` singleton is resolved-once-and-cached from the
    environment (PROTOAGENT_HOME / PROTOAGENT_INSTANCE / PROTOAGENT_BOX_ROOT), so a
    test that sets one of those vars (or monkeypatches ``data_home``) needs the
    cache cleared or it'd read a stale path. Reset BEFORE (so a test's env is seen
    on the first ``instance_paths()`` call) and AFTER (so a stale cache never leaks
    into the next test)."""
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
