"""Ensure deterministic import resolution for the protoagent test suite.

Moves site-packages to the front of sys.path so installed packages
(langchain_core, langchain, etc.) are never shadowed by local directories
that pytest inserts during collection.
"""
from __future__ import annotations

import os
import site
import sys


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
