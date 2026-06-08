"""GitHub read tools are opt-in via the first-party `github` plugin (lean-core
audit) — not in the default tool set, registered when the plugin is enabled."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_github_tools_not_in_default_tool_set():
    from tools.lg_tools import get_all_tools

    names = {t.name for t in get_all_tools()}
    assert not any(n.startswith("github_") for n in names), names


def test_github_plugin_registers_the_read_tools():
    # plugins/ isn't a package; load the entry module by path (like the loader).
    init = Path("plugins/github/__init__.py")
    spec = importlib.util.spec_from_file_location("_test_github_plugin", init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeRegistry:
        def __init__(self):
            self.tools = []

        def register_tools(self, tools):
            self.tools.extend(tools)

    reg = FakeRegistry()
    mod.register(reg)
    names = {t.name for t in reg.tools}
    assert names == {
        "github_get_pr", "github_get_issue", "github_list_issues", "github_get_commit_diff",
    }
