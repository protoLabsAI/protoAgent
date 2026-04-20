"""Ensure deterministic import resolution for the protoagent test suite.

Moves site-packages to the front of sys.path so installed packages
(langchain_core, langchain, etc.) are never shadowed by local directories
that pytest inserts during collection.
"""
from __future__ import annotations

import site
import sys


def pytest_configure(config):  # noqa: ARG001
    """Prepend site-packages to sys.path before any test imports occur."""
    site_dirs = site.getsitepackages()
    for sp in reversed(site_dirs):
        if sp in sys.path:
            sys.path.remove(sp)
        sys.path.insert(0, sp)
