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

    # Isolate the ADR-0047 Host layer: default PROTOAGENT_HOST_CONFIG to an absent
    # path so from_yaml sees no host-config.yaml unless a test opts in (the cascade
    # then collapses to App defaults + the agent leaf — today's behavior).
    # Deterministic regardless of any host-config.yaml on the dev/CI machine.
    # `setdefault` lets cascade tests override via monkeypatch.setenv.
    os.environ.setdefault("PROTOAGENT_HOST_CONFIG", "/nonexistent/protoagent-host-config.test.yaml")
