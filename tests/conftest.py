"""Diagnostic conftest (temporary) — print import resolution under pytest."""
from __future__ import annotations

import importlib.util
import sys


def pytest_configure(config):  # noqa: ARG001
    print("\n[conftest] sys.path:")
    for p in sys.path:
        print("   ", repr(p))
    spec = importlib.util.find_spec("langchain_core")
    print("[conftest] langchain_core origin:", spec.origin if spec else None)
    print("[conftest] langchain_core search_locations:",
          list(spec.submodule_search_locations)
          if spec and spec.submodule_search_locations else None)
