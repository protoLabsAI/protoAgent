"""GitHub read tools as a first-party plugin (lean-core audit).

The read-only GitHub tools (PR / issue / list-issues / commit-diff over the `gh`
CLI) aren't universal, so they're opt-in rather than shipped in the default tool
set. The implementation stays in ``tools/github_tools.py`` (a shared library that
uses ``tools/gh_cli.py``); this plugin just registers it. Enable with
``plugins: { enabled: [github] }``.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def register(registry) -> None:
    from tools.github_tools import get_github_tools

    registry.register_tools(get_github_tools())
    log.info("[plugins] github: registered %d read tools", 4)
