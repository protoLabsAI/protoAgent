"""Plugin system — drop-in packages that extend the agent.

A plugin is a directory with a ``protoagent.plugin.yaml`` manifest and a Python
module exposing ``register(registry)``. Enabled plugins run **in-process** with
the agent's privileges (the trusted, opt-in model — see docs/guides/plugins.md),
so they're disabled by default and an operator opts in explicitly.

This slice supports tool and bundled-skill contributions; subagent and
middleware contributions are planned follow-ups.
"""

from graph.plugins.loader import PluginLoadResult, load_plugins
from graph.plugins.manifest import PluginManifest
from graph.plugins.registry import PluginRegistry

__all__ = ["PluginLoadResult", "PluginManifest", "PluginRegistry", "load_plugins"]
