"""Plugin/bundle scaffolding — the writers behind the `scaffold_plugin` devkit
tool AND the `plugin new` / `plugin new-bundle` CLI (ADR 0027).

These are pure filesystem writers: no agent, no graph, no enable. They drop a
ready-to-fill skeleton on disk and return what they wrote. Living in core (not
the plugin) means the CLI can scaffold even when the devkit plugin is disabled.
The *live-enable* half — turning the skeleton on and hot-reloading so you can
test it without a restart — lives in the plugin-devkit tool, because it needs
the running graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ── stubs (the worked templates) ────────────────────────────────────────────

_INIT_STUB = '''"""{name} — a protoAgent plugin (scaffolded by plugin-devkit)."""

from __future__ import annotations

from langchain_core.tools import tool
{view_import}

def register(registry):
    """Wire this plugin's contributions into the agent (ADR 0018)."""
{registrations}
    # Event bus (ADR 0039) — coordinate without importing other plugins:
    #   registry.emit("did_something", {{"id": 1}})   # → "{id_us}.did_something" on the bus
    #   registry.on("other-plugin.*", lambda evt: ...) # react to anyone's events
'''

_TOOL_STUB = '''
    @tool
    def {id_us}_hello(name: str = "world") -> str:
        """Say hello — replace with your tool's real work."""
        return f"hello, {{name}}, from {id}"
    registry.register_tool({id_us}_hello)
'''

_VIEW_STUB = '''
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
    router = APIRouter()

    @router.get("/view")
    async def _view():
        # Four rules (ADR 0026/0038): serve the declared path · gate DATA (not the page)
        # · slug-aware base · link the DS kit. Untrusted/generated HTML → nest it in an
        # <iframe sandbox="allow-scripts"> with NO same-origin.
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<script>var B=location.pathname.split('/plugins/')[0];"  # "" on host, /agents/<slug> via proxy
            "var l=document.createElement('link');l.rel='stylesheet';"
            "l.href=B+'/_ds/plugin-kit.css';document.head.appendChild(l);</script>"
            "<style>body{{margin:0;padding:32px;background:var(--pl-color-bg);"
            "color:var(--pl-color-fg);font-family:var(--pl-font-sans,system-ui)}}</style>"
            "</head><body><h1>{name}</h1>"
            "<p>Your plugin view — replace this page. Load the kit JS and fetch gated data "
            "with kit.apiFetch('/api/plugins/{id}/...').</p></body></html>"
        )
    # The PAGE is PUBLIC: an iframe page-load can't carry a bearer, so it must NOT be
    # gated. Mount any DATA routes under /api/plugins/{id} for the operator bearer gate.
    registry.register_router(router, prefix="/plugins/{id}")
'''

_MANIFEST_STUB = """id: {id}
name: {name}
version: 0.1.0
description: >-
  {summary}
enabled: false
config_section: {id_us}
# Event bus (ADR 0039) — topics this plugin broadcasts / listens for (optional, for discovery):
# emits: ["{id_us}.something"]
# subscribes: ["other-plugin.*"]
{views_block}"""

_SKILL_STUB = """---
name: {id}-skill
description: >-
  Describe WHEN to use this skill (the trigger). Replace this with the cases that
  should invoke {name}.
---

# {name}

Replace this body with the procedure the agent should follow.
"""

_WORKFLOW_STUB = """name: {id}-workflow
description: A scaffolded workflow — replace the steps with your recipe.
version: 1
inputs:
  - name: request
    description: What to do.
    required: true
steps:
  - id: do
    subagent: researcher
    prompt: |
      {{{{inputs.request}}}}
output: "{{{{steps.do.output}}}}"
"""

# Shippable-repo stubs (with_tests) — a host-free test suite + CI + dev deps, so a
# standalone-repo plugin is green from birth. Written verbatim unless noted.
_CONFTEST_STUB = '''"""Test bootstrap — load the plugin host-free (no protoAgent running).

Executing __init__.py is safe: the host-only imports (fastapi, graph.*) are lazy
(inside register()), so the suite needs only requirements-dev.txt.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def plugin():
    spec = importlib.util.spec_from_file_location("plugin_under_test", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Registry:
    """A fake registry — records what register() contributes, with no host."""

    def __init__(self):
        self.config = {}
        self.tools, self.routers, self.surfaces, self.subagents, self.skill_dirs = [], [], [], [], []

    def register_tool(self, t):
        self.tools.append(t)

    def register_tools(self, ts):
        self.tools.extend(ts)

    def register_router(self, router, prefix=""):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None):
        self.surfaces.append(name)

    def register_subagent(self, cfg):
        self.subagents.append(cfg)

    def register_skill_dir(self, path):
        self.skill_dirs.append(path)

    def emit(self, *a, **k):
        pass

    def on(self, *a, **k):
        pass


@pytest.fixture
def registry():
    return _Registry()
'''

# .format(id=, name=) — no other literal braces.
_TEST_STUB = '''"""Smoke tests for {name} — host-free (no protoAgent running)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_register_runs_host_free(plugin, registry):
    plugin.register(registry)  # must not raise with no host present
    # The scaffold wires its contributions here — replace with your real assertions.
    assert registry.tools or registry.routers or registry.surfaces or registry.subagents


def test_manifest_is_valid():
    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert m["id"] == "{id}" and m["version"]
'''

# Verbatim (the ${{ }} is GitHub Actions syntax — do NOT .format this).
_CI_STUB = """name: CI

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  test:
    # The org's Namespace Linux profile; override NSC_RUNNER on a fork that lacks it.
    runs-on: ${{ vars.NSC_RUNNER || 'namespace-profile-protolabs-linux' }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements-dev.txt ruff
      # Lint the whole plugin; format-check only tests/ (the generated starter
      # __init__ is yours to shape). Broaden to `ruff format --check .` once you've
      # run `ruff format .` on your code.
      - run: ruff check . && ruff format --check tests/
      - run: pytest -q
"""

_REQS_DEV_STUB = """# Test-only deps — the host (protoAgent) provides langchain-core + fastapi at
# runtime; these let the suite run standalone in CI with no host.
fastapi>=0.110
langchain-core>=0.2
pyyaml>=6
httpx>=0.27
pytest>=8
# pytest-asyncio>=0.23   # uncomment if you add async tests (asyncio_mode below)
"""

# .format(id=) — no literal braces in this TOML.
_PYPROJECT_STUB = """[project]
name = "{id}"
version = "0.1.0"            # keep in lockstep with protoagent.plugin.yaml
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
# asyncio_mode = "auto"     # uncomment with pytest-asyncio for async tests

[tool.ruff]
line-length = 120
target-version = "py311"
"""


# Communication plugin (ADR 0029) — a ChatAdapter on the shared wirer.
_COMMS_MANIFEST_STUB = """id: {id}
name: {name}
version: 0.1.0
description: >-
  {summary}
enabled: false
config_section: {id}
config:
  enabled: false
  admin_ids: []
secrets: [bot_token]
settings:
  - {{ key: enabled, label: "Enable {name}", type: bool, description: "Inbound gateway. Needs the token below; reconnects live on save." }}
  - {{ key: bot_token, label: "Bot token", type: secret, description: "Platform bot token. Use Test connection to verify." }}
  - {{ key: admin_ids, label: "Admin user IDs", type: string_list, description: "User IDs allowed to message the bot (one per line). Empty = anyone." }}
"""

_COMMS_INIT_STUB = '''"""{name} — a communication plugin (scaffolded by plugin-devkit, ADR 0029).

Implement the transport (connect / receive / send); the shared wirer handles
admin-gating, per-conversation threads, agent invoke, reply-chunking, lifecycle,
and the Test route. Reference: plugins/telegram. Guide:
docs/guides/communication-plugins.md.
"""

from __future__ import annotations

from graph.plugins.chat_surface import InboundMessage, register_chat_surface


class {cls}Adapter:
    id = "{id}"
    chunk_limit = 4000  # your platform's message-length cap (0 = no chunking)

    def configured(self, cfg) -> bool:
        return bool((cfg.get("bot_token") or "").strip())

    async def validate(self, cfg):
        """Verify the token (the Test button). Return (ok, identity|None, error|None)."""
        token = (cfg.get("bot_token") or "").strip()
        if not token:
            return (False, None, "No bot token set.")
        # TODO: call your platform's auth/me endpoint and return its identity.
        return (True, None, None)

    async def run(self, handle, *, cfg, host):
        """Connect, then loop. For each inbound message:
            async def reply(text): ...send it back...
            await handle(InboundMessage(text=..., user_id=..., channel_id=..., reply=reply))
        Runs until cancelled."""
        raise NotImplementedError("Implement {cls}Adapter.run — see plugins/telegram.")


def register(registry):
    register_chat_surface(registry, {cls}Adapter())
'''

# Bundle (ADR 0040) — a reference manifest naming a set of plugins to install +
# enable together. No code of its own; each member is a git URL or builtin: true.
_BUNDLE_STUB = """id: {id}
name: {name}
description: >-
  {summary}
# A bundle NAMES plugins to install + enable together (ADR 0040). It carries no
# code — each member is a git {{url, ref}} or `builtin: true` (ships with protoAgent).
plugins:
{members}
enabled: [{enabled}]
# Per-member config applied on install (optional):
# config:
#   some_plugin: {{ setting: value }}
"""

# Single braces: this is a .format() ARGUMENT (the members block), not the format
# string, so its braces are NOT un-escaped — they must already be literal YAML.
_BUNDLE_MEMBER_PLACEHOLDER = (
    "  # - { id: delegates,  builtin: true }                      # ships with protoAgent\n"
    "  - { id: REPLACE_ME, url: https://github.com/you/your-plugin, ref: v0.1.0 }"
)


# ── helpers ──────────────────────────────────────────────────────────────────


def slug(name: str) -> str:
    """A plugin/bundle id: lowercase, dash-separated, safe for a dir + module name."""
    return re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-") or "plugin"


@dataclass
class Scaffolded:
    """What a scaffold wrote — for the CLI to print or the tool to narrate."""

    id: str
    path: Path
    made: list[str] = field(default_factory=list)
    kind: str = "plugin"  # "plugin" | "comms" | "bundle"


def live_plugins_dir() -> Path:
    """The plugins dir the loader discovers — the default scaffold target."""
    from graph.plugins.installer import live_plugins_dir as _d

    return _d()


def _resolve_root(target_dir: str | None) -> Path:
    return Path(target_dir).expanduser() if target_dir else live_plugins_dir()


def _class_name(name: str, fallback: str) -> str:
    parts = [w for w in re.split(r"[-_ ]+", name) if w]
    return "".join(w[:1].upper() + w[1:] for w in parts) or fallback.title()


# ── writers ──────────────────────────────────────────────────────────────────


def scaffold_plugin(
    name: str,
    *,
    summary: str = "A protoAgent plugin.",
    with_tool: bool = True,
    with_view: bool = False,
    with_skill: bool = False,
    with_workflow: bool = False,
    with_comms: bool = False,
    with_tests: bool = False,
    target_dir: str | None = None,
) -> Scaffolded:
    """Write a new plugin SKELETON (manifest + ``register()`` + optional
    view/skill/workflow stubs) under ``target_dir`` (or the live plugins dir).

    ``with_comms=True`` writes a **communication plugin** (ADR 0029) — a
    ``ChatAdapter`` skeleton on the shared wirer instead of the default tool
    plugin. ``with_tests=True`` also writes a **host-free test suite + CI +
    requirements-dev + pyproject** so a standalone-repo plugin is green from birth
    (skip it for a plugin bundled inside protoAgent, which uses the host's tests/CI;
    not written for ``with_comms`` plugins, which import the host at module top).
    Raises ``FileExistsError`` if the id already exists.
    """
    pid = slug(name)
    id_us = pid.replace("-", "_")
    target = _resolve_root(target_dir) / pid
    if target.exists():
        raise FileExistsError(str(target))
    target.mkdir(parents=True)

    # Communication plugin (ADR 0029) — different shape from the default tool plugin.
    if with_comms:
        cls = _class_name(name, id_us)
        (target / "protoagent.plugin.yaml").write_text(
            _COMMS_MANIFEST_STUB.format(id=pid, name=name, summary=summary)
        )
        (target / "__init__.py").write_text(
            _COMMS_INIT_STUB.format(id=pid, name=name, cls=cls)
        )
        made = ["protoagent.plugin.yaml", "__init__.py (ChatAdapter)"]
        if with_skill:
            sk = target / "skills" / f"{pid}-skill"
            sk.mkdir(parents=True)
            (sk / "SKILL.md").write_text(_SKILL_STUB.format(id=pid, name=name))
            made.append("skills/")
        return Scaffolded(id=pid, path=target, made=made, kind="comms")

    views_block = (
        f"views:\n  - {{ id: main, label: \"{name}\", icon: Boxes, path: /plugins/{pid}/view }}\n"
        if with_view else ""
    )
    (target / "protoagent.plugin.yaml").write_text(
        _MANIFEST_STUB.format(id=pid, name=name, summary=summary, id_us=id_us, views_block=views_block)
    )

    registrations = ""
    if with_tool:
        registrations += _TOOL_STUB.format(id=pid, id_us=id_us)
    if with_view:
        registrations += _VIEW_STUB.format(id=pid, name=name)
    if not registrations.strip():
        registrations = "    pass  # add registry.register_* calls here\n"
    (target / "__init__.py").write_text(
        _INIT_STUB.format(name=name, view_import="", registrations=registrations, id_us=id_us)
    )

    made = ["protoagent.plugin.yaml", "__init__.py"]
    if with_skill:
        sk = target / "skills" / f"{pid}-skill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(_SKILL_STUB.format(id=pid, name=name))
        made.append("skills/")
    if with_workflow:
        (target / "workflows").mkdir()
        (target / "workflows" / f"{pid}.yaml").write_text(_WORKFLOW_STUB.format(id=pid))
        made.append("workflows/")
    if with_tests:
        _write_test_harness(target, pid=pid, id_us=id_us, name=name)
        made += ["tests/", ".github/workflows/ci.yml", "requirements-dev.txt", "pyproject.toml"]

    return Scaffolded(id=pid, path=target, made=made, kind="plugin")


def _write_test_harness(target: Path, *, pid: str, id_us: str, name: str) -> None:
    """Write the host-free test suite + CI + dev deps + pyproject (with_tests)."""
    tdir = target / "tests"
    tdir.mkdir()
    (tdir / "conftest.py").write_text(_CONFTEST_STUB)
    (tdir / f"test_{id_us}.py").write_text(_TEST_STUB.format(id=pid, name=name))
    gh = target / ".github" / "workflows"
    gh.mkdir(parents=True)
    (gh / "ci.yml").write_text(_CI_STUB)
    (target / "requirements-dev.txt").write_text(_REQS_DEV_STUB)
    (target / "pyproject.toml").write_text(_PYPROJECT_STUB.format(id=pid))


def _render_members(members: list[dict] | None) -> tuple[str, list[str]]:
    """Render the bundle's ``plugins:`` block + the list of member ids (for enabled)."""
    if not members:
        return _BUNDLE_MEMBER_PLACEHOLDER, ["REPLACE_ME"]
    lines, ids = [], []
    for m in members:
        # Member ids reference EXISTING plugin ids — keep them verbatim (plugin ids
        # keep underscores, e.g. project_board / agent_browser); don't slugify.
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        ids.append(mid)
        if m.get("builtin"):
            lines.append(f"  - {{ id: {mid}, builtin: true }}")
        else:
            url = m.get("url") or "https://github.com/you/your-plugin"
            ref = m.get("ref")
            ref_part = f", ref: {ref}" if ref else ""
            lines.append(f"  - {{ id: {mid}, url: {url}{ref_part} }}")
    return "\n".join(lines), ids


def scaffold_bundle(
    name: str,
    *,
    summary: str = "A protoAgent plugin bundle.",
    members: list[dict] | None = None,
    enabled: list[str] | None = None,
    target_dir: str | None = None,
) -> Scaffolded:
    """Write a ``protoagent.bundle.yaml`` skeleton (ADR 0040) under a new
    ``<bundle-id>/`` dir. ``members`` is a list of ``{id, url, ref}`` or
    ``{id, builtin: true}``; with none, a REPLACE_ME template is written.
    Raises ``FileExistsError`` if the id already exists.
    """
    bid = slug(name)
    target = _resolve_root(target_dir) / bid
    if target.exists():
        raise FileExistsError(str(target))
    target.mkdir(parents=True)
    members_block, member_ids = _render_members(members)
    enabled_ids = enabled if enabled is not None else member_ids
    (target / "protoagent.bundle.yaml").write_text(
        _BUNDLE_STUB.format(
            id=bid, name=name, summary=summary,
            members=members_block, enabled=", ".join(enabled_ids),
        )
    )
    return Scaffolded(id=bid, path=target, made=["protoagent.bundle.yaml"], kind="bundle")
