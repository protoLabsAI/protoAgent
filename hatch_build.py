"""Custom Hatchling build hook — bundle the read-only runtime seeds into the wheel.

These assets (config seeds, `static/`, the console SPA `apps/web/dist`) are what the
runtime resolves against the bundle root (`infra.paths._app_root` / `server._bundle_root`),
so shipping them makes `uv tool install protoagent` a working runtime + console — not just
the import surface. It mirrors the frozen desktop bundle (apps/desktop/sidecar/build_sidecar.py
`_ASSETS`).

Why a hook instead of a static `[tool.hatch.build.targets.wheel.force-include]`:

- **`apps/web/dist` is a build artifact** (gitignored), absent until the frontend is built.
  A static force-include is a HARD error on a missing path, which would break every
  `pip install -e .` / source install done before `npm run build` — including CI's test
  jobs (`pip install -r requirements.txt` → `-e .`). Here each seed is added only if it
  exists on disk, so a source install with no built frontend still works (console-less).
- **Editable installs don't need bundled copies** — they point back at the source tree,
  which already has these files. We skip them entirely (`version != "standard"`).

Release builds MUST build the frontend first so `apps/web/dist` is present; otherwise the
wheel ships without a console (the release runbook asserts it exists before `uv build`).
"""

from __future__ import annotations

import os

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# src (repo-relative) -> dest (wheel-relative, under the bundle root). Keep in step with
# apps/desktop/sidecar/build_sidecar.py `_ASSETS`.
_SEEDS: dict[str, str] = {
    "config/langgraph-config.example.yaml": "config/langgraph-config.example.yaml",
    "config/SOUL.md": "config/SOUL.md",
    "config/plugin-catalog.json": "config/plugin-catalog.json",
    "config/mcp-catalog.json": "config/mcp-catalog.json",
    "config/soul-presets": "config/soul-presets",
    "config/skills": "config/skills",
    "static": "static",
    "apps/web/dist": "apps/web/dist",
}


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        # Only the built wheel ships bundled seeds; an editable install resolves them
        # from the live source tree, so skip it (and never hard-fail on a missing artifact).
        if version != "standard":
            return
        force_include = build_data.setdefault("force_include", {})
        for src, dest in _SEEDS.items():
            if os.path.exists(os.path.join(self.root, src)):
                force_include[src] = dest
