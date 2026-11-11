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
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ── stubs (the worked templates) ────────────────────────────────────────────

_INIT_STUB = '''"""{name} — a protoAgent plugin (scaffolded by plugin-devkit)."""

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

_VIEW_STUB = """
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse, JSONResponse
    router = APIRouter()

    @router.get("/view")
    async def _view():
        # Four rules (ADR 0026/0038): serve the declared path · gate DATA (not the page)
        # · slug-aware base · link the DS kit (CSS + JS). Untrusted/generated HTML → nest
        # it in an <iframe sandbox="allow-scripts"> with NO same-origin.
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            # Slug-aware base: "" on the host, "/agents/<slug>" through the fleet proxy.
            "<script>window.__base=location.pathname.split('/plugins/')[0];"
            "var l=document.createElement('link');l.rel='stylesheet';"
            "l.href=window.__base+'/_ds/plugin-kit.css';document.head.appendChild(l);</script>"
            "<style>body{{margin:0;padding:32px;background:var(--pl-color-bg);"
            "color:var(--pl-color-fg);font-family:var(--pl-font-sans,system-ui)}}</style>"
            "</head><body><h1>{name}</h1><p id='out'>Loading…</p>"
            # plugin-kit.js is an ES module — a dynamic import() carries the slug-aware base.
            # initPluginView() runs the token/theme handshake; apiFetch() is slug-aware AND
            # attaches the bearer, so gated /api/plugins/{id}/* data loads under a token gate.
            "<script type='module'>"
            "const kit=await import(window.__base+'/_ds/plugin-kit.js');"
            "kit.initPluginView();"
            "const r=await kit.apiFetch('/api/plugins/{id}/hello');"
            "document.getElementById('out').textContent=(await r.json()).message;"
            "</script></body></html>"
        )
    # Two-router pattern (ADR 0026): the PAGE is PUBLIC (an iframe page-load can't carry a
    # bearer), so it mounts under /plugins/{id}; its DATA is gated under /api/plugins/{id},
    # which the operator bearer protects and kit.apiFetch() authenticates.
    registry.register_router(router, prefix="/plugins/{id}")

    data = APIRouter()

    @data.get("/hello")
    async def _hello():
        return JSONResponse({{"message": "Hello from {name}!"}})
    registry.register_router(data, prefix="/api/plugins/{id}")
"""

_BUS_VIEW_STUB = '''
    from pathlib import Path
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import HTMLResponse

    latest_reply = {{}}

    def on_reply(event):
        latest_reply.clear()
        latest_reply.update(event.get("data", {{}}))

    registry.on("{id}.reply", on_reply)

    @tool
    def {id_us}_show(message: str) -> str:
        """Display a message in the console view; then use the reply tool."""
        latest_reply.clear()
        registry.emit("{id}.show", {{"message": message}})
        registry.navigate("main")
        return "View requested. Read the reply after the view renders."

    @tool
    def {id_us}_reply() -> dict:
        """Read the latest view acknowledgement or uploaded screenshot reference."""
        return dict(latest_reply)

    registry.register_tool({id_us}_show)
    registry.register_tool({id_us}_reply)
    page = APIRouter()

    @page.get("/view")
    async def view():
        return HTMLResponse((Path(__file__).parent / "views" / "main.html").read_text())

    registry.register_router(page, prefix="/plugins/{id}")
    data = APIRouter()

    @data.post("/reply")
    async def binary_reply(request: Request):
        # The host operator gate protects /api/plugins/*; never exempt this prefix.
        # Stream the body so missing or dishonest Content-Length cannot bypass the cap.
        limit = 2 * 1024 * 1024
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(413, "Reply exceeds 2 MiB")
            body.extend(chunk)
        if not body:
            raise HTTPException(400, "Empty reply")
        if request.headers.get("content-type", "").split(";")[0] != "image/png":
            raise HTTPException(415, "Expected image/png")
        ref = registry.save_media(bytes(body), "image/png")
        reply = {{"url": ref.url, "mime": ref.mime}}
        on_reply({{"data": reply}})
        registry.emit("{id}.reply", reply)
        return reply

    registry.register_router(data, prefix="/api/plugins/{id}")
'''

_BUS_PAGE_STUB = '''<!doctype html>
<html><head><meta charset="utf-8"><title>{name}</title>
<script>
window.__base = location.pathname.split('/plugins/')[0];
const css = document.createElement('link');
css.rel = 'stylesheet';
css.href = window.__base + '/_ds/plugin-kit.css';
document.head.appendChild(css);
</script>
<style>
body {{ margin: 0; padding: 32px; background: var(--pl-color-bg);
       color: var(--pl-color-fg); font-family: var(--pl-font-sans, system-ui); }}
</style></head>
<body><h1>{name}</h1><p id="out">Waiting for the agent…</p>
<script type="module">
const kit = await import(window.__base + '/_ds/plugin-kit.js');
kit.initPluginView();
const stop = kit.subscribe('{id}.show', (event) => {{
    document.getElementById('out').textContent = event.data.message;
    kit.publish('{id}.reply', {{ message: event.data.message, rendered: true }});
}});
window.addEventListener('pagehide', stop, {{ once: true }});

// For a canvas screenshot, call uploadScreenshot(await canvasBlob) after rendering.
// Binary data travels through the gated route, never through the event bus.
async function uploadScreenshot(blob) {{
    const response = await kit.apiFetch('/api/plugins/{id}/reply', {{
        method: 'POST', headers: {{ 'Content-Type': 'image/png' }}, body: blob
    }});
    if (!response.ok) throw new Error('Screenshot upload failed: ' + response.status);
    return response.json();
}}
</script></body></html>
'''

_BUS_TEST_STUB = '''"""Agent-driven view contract — runs with the vendored testkit."""

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_emit_before_navigate_and_reply(plugin, registry):
    plugin.register(registry)
    tools = {{tool.name: tool for tool in registry.tools}}
    with patch.object(registry, "emit") as emit, patch.object(registry, "navigate") as navigate:
        calls = []
        emit.side_effect = lambda *args: calls.append(("emit", args))
        navigate.side_effect = lambda *args: calls.append(("navigate", args))
        tools["{id_us}_show"].invoke({{"message": "hello"}})
    assert calls == [
        ("emit", ("{id}.show", {{"message": "hello"}})),
        ("navigate", ("main",)),
    ]
    registry.handlers["{id}.reply"][0]({{"data": {{"rendered": True}}}})
    assert tools["{id_us}_reply"].invoke({{}}) == {{"rendered": True}}
    tools["{id_us}_show"].invoke({{"message": "next"}})
    assert tools["{id_us}_reply"].invoke({{}}) == {{}}


def test_page_and_binary_reply(plugin, registry):
    registry.plugin_id = "{id}"
    plugin.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    # Production supplies operator auth for /api/plugins/*; testkit mounts routes only.
    with TestClient(app) as client:
        page = client.get("/plugins/{id}/view")
        assert page.status_code == 200
        assert "kit.subscribe('{id}.show'" in page.text
        assert "kit.publish('{id}.reply'" in page.text
        assert "postMessage" not in page.text
        url = "/api/plugins/{id}/reply"
        headers = {{"Content-Type": "image/png"}}
        assert client.post(url, content=b"", headers=headers).status_code == 400
        assert client.post(url, content=b"x", headers={{"Content-Type": "text/plain"}}).status_code == 415
        assert client.post(url, content=b"x" * (2 * 1024 * 1024 + 1), headers=headers).status_code == 413
        assert not registry.saved_media
        response = client.post(url, content=b"png", headers=headers)
        assert response.status_code == 200
        assert registry.saved_media[0][:2] == (b"png", "image/png")
        assert registry.emitted[-1] == ("{id}.reply", response.json())


def test_manifest_topics():
    import yaml

    root = Path(__file__).resolve().parent.parent
    manifest = yaml.safe_load((root / "protoagent.plugin.yaml").read_text())
    assert manifest["emits"] == ["{id}.show", "{id}.reply"]
    assert manifest["subscribes"] == ["{id}.show", "{id}.reply"]
'''

_MANIFEST_STUB = """id: {id}
name: {name}
version: 0.1.0
description: >-
  {summary}
enabled: false
config_section: {id_us}
# Event bus (ADR 0039) — topics this plugin broadcasts / listens for (optional, for discovery).
# An emits entry may also declare its payload shape (typed event contracts, #1636):
# emits: ["{id_us}.something"]
#   # - topic: {id_us}.something
#   #   summary: One-line meaning of the event
#   #   schema: {{type: object}}          # inline JSON Schema, or {{$ref: events/foo.json}}
# subscribes: ["other-plugin.*"]
{views_block}"""

_SKILL_STUB = """---
name: {id}-skill
description: Use {name} when the request needs this plugin's tools or view.
---

# Using {name}

## When to use it
Use this plugin for its declared capabilities. Check its tool descriptions before
acting; replace this paragraph with domain-specific triggers as you build it.

## Tool order
1. Check that the plugin is enabled and its required settings are configured.
2. Call the task tool with the user's inputs. For a bus view, call
   `{id_us}_show` to emit the request and open the view, then `{id_us}_reply`
   after rendering to read the acknowledgement or screenshot reference.
3. Report the result; an empty reply means no acknowledgement has arrived yet.

## Failure modes
- Missing tools: enable the plugin and reload it before retrying.
- Missing configuration or credentials: configure Settings before invoking tools.
- View unavailable or no reply: open the console view and check its load error;
  do not poll or start a side server.
- Binary reply rejected: use PNG and keep the upload at or below 2 MiB.
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

Uses the vendored testkit (``_plugin_testkit.py`` — a verbatim copy of protoAgent's
``graph/plugins/testkit.py``, dropped here so the suite has zero protoAgent dependency).
It loads the plugin as a PACKAGE, so your tests can import and exercise the REAL sibling
modules — relative imports and all — not just ``register()``:

    def test_engine(plugin):
        import importlib
        engine = importlib.import_module(plugin.__name__ + ".engine")  # a deep module
        assert engine.classify([...]) == ...

``install_host_stubs()`` (run at import below) registers stand-ins for the host-only
modules (``graph.*`` / ``knowledge.*``) so a plugin module that imports them loads with no
host; monkeypatch a seam to assert its behaviour. To refresh the vendored copy after a
protoAgent upgrade, re-run ``scaffold_plugin`` or recopy ``graph/plugins/testkit.py``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `import _plugin_testkit` resolves

from _plugin_testkit import FakeRegistry, install_host_stubs, load_plugin  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ID = "{id}"

install_host_stubs()  # before any plugin module imports the host


@pytest.fixture
def plugin():
    """The plugin loaded as a package — ``register()`` runs and ``from <pkg> import x``
    works, so you can unit-test deep engine modules, not just registration."""
    return load_plugin(ROOT, PLUGIN_ID)


@pytest.fixture
def registry():
    """A fake registry that records what ``register()`` contributes (assert against it)."""
    return FakeRegistry()
'''

# .format(id=, name=) — no other literal braces.
_TEST_STUB = '''"""Smoke tests for {name} — host-free (no protoAgent running)."""

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
    runs-on: ubuntu-latest
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
    with_view: bool | str = False,
    with_skill: bool = True,
    with_workflow: bool = False,
    with_comms: bool = False,
    with_tests: bool = True,
    git_init: bool = False,
    target_dir: str | None = None,
) -> Scaffolded:
    """Write a new plugin SKELETON (manifest + ``register()`` + optional
    view/skill/workflow stubs) under ``target_dir`` (or the live plugins dir).

    ``with_view="bus"`` adds an emit-then-navigate tool, kit bus page and gated
    binary reply route. True and "basic" retain the basic view. Tests and a usage
    skill are included by default; pass False explicitly to omit either.

    ``with_comms=True`` writes a **communication plugin** (ADR 0029) — a
    ``ChatAdapter`` skeleton on the shared wirer instead of the default tool
    plugin. ``with_tests=True`` also writes a **host-free test suite + CI +
    requirements-dev + pyproject** so a standalone-repo plugin is green from birth
    (skip it for a plugin bundled inside protoAgent, which uses the host's tests/CI;
    not written for ``with_comms`` plugins, which import the host at module top).
    ``git_init=True`` makes the scaffold a **repo from birth** (ADR 0096 D6): init +
    an initial commit, so a plugin meant to outlive its first session has history
    and can graduate to a remote/board later. Raises ``FileExistsError`` if the id
    already exists.
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
        (target / "protoagent.plugin.yaml").write_text(_COMMS_MANIFEST_STUB.format(id=pid, name=name, summary=summary))
        (target / "__init__.py").write_text(_COMMS_INIT_STUB.format(id=pid, name=name, cls=cls))
        made = ["protoagent.plugin.yaml", "__init__.py (ChatAdapter)"]
        if with_skill:
            sk = target / "skills" / f"{pid}-skill"
            sk.mkdir(parents=True)
            (sk / "SKILL.md").write_text(_SKILL_STUB.format(id=pid, name=name, id_us=id_us))
            made.append("skills/")
        if git_init:
            _maybe_git_init(target, made)
        return Scaffolded(id=pid, path=target, made=made, kind="comms")

    bus_view = with_view == "bus"
    views_block = (
        f'views:\n  - {{ id: main, label: "{name}", icon: Boxes, path: /plugins/{pid}/view }}\n' if with_view else ""
    )
    if bus_view:
        views_block += (
            f'emits: ["{pid}.show", "{pid}.reply"]\n'
            f'subscribes: ["{pid}.show", "{pid}.reply"]\n'
        )
    (target / "protoagent.plugin.yaml").write_text(
        _MANIFEST_STUB.format(id=pid, name=name, summary=summary, id_us=id_us, views_block=views_block)
    )

    registrations = ""
    if with_tool:
        registrations += _TOOL_STUB.format(id=pid, id_us=id_us)
    if bus_view:
        registrations += _BUS_VIEW_STUB.format(id=pid, id_us=id_us)
        (target / "views").mkdir()
        (target / "views" / "main.html").write_text(_BUS_PAGE_STUB.format(id=pid, name=name))
    elif with_view:
        registrations += _VIEW_STUB.format(id=pid, name=name)
    if not registrations.strip():
        registrations = "    pass  # add registry.register_* calls here\n"
    (target / "__init__.py").write_text(
        _INIT_STUB.format(name=name, view_import="", registrations=registrations, id_us=id_us)
    )

    made = ["protoagent.plugin.yaml", "__init__.py"]
    if bus_view:
        made.append("views/main.html")
    if with_skill:
        sk = target / "skills" / f"{pid}-skill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(_SKILL_STUB.format(id=pid, name=name, id_us=id_us))
        made.append("skills/")
    if with_workflow:
        (target / "workflows").mkdir()
        (target / "workflows" / f"{pid}.yaml").write_text(_WORKFLOW_STUB.format(id=pid))
        made.append("workflows/")
    if with_tests:
        _write_test_harness(target, pid=pid, id_us=id_us, name=name)
        if bus_view:
            (target / "tests" / "test_bus_view.py").write_text(_BUS_TEST_STUB.format(id=pid, id_us=id_us))
        made += ["tests/", ".github/workflows/ci.yml", "requirements-dev.txt", "pyproject.toml"]
    if git_init:
        _maybe_git_init(target, made)

    return Scaffolded(id=pid, path=target, made=made, kind="plugin")


def _maybe_git_init(target: Path, made: list[str]) -> None:
    """Make the scaffold a git repo with an initial commit (ADR 0096 D6). Best-effort:
    no git on PATH or a failed command is NOTED in ``made``, never fatal — the skeleton
    already landed. The commit carries an explicit identity so it works on hosts with
    no global git config (CI, fresh machines)."""
    if shutil.which("git") is None:
        made.append("(git not on PATH — repo not initialized)")
        return
    try:
        for cmd in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=protoagent-devkit",
                "-c",
                "user.email=devkit@protoagent.local",
                "commit",
                "-q",
                "-m",
                "scaffold: initial skeleton (plugin-devkit)",
            ],
        ):
            subprocess.run(cmd, cwd=target, check=True, capture_output=True, text=True, timeout=30)
        made.append(".git (initial commit)")
    except Exception as exc:  # noqa: BLE001 — repo-from-birth is a bonus, not a gate
        made.append(f"(git init failed: {exc})")


def _write_test_harness(target: Path, *, pid: str, id_us: str, name: str) -> None:
    """Write the host-free test suite + CI + dev deps + pyproject (with_tests).

    Vendors ``graph/plugins/testkit.py`` verbatim as ``tests/_plugin_testkit.py`` (single
    source of truth — stdlib-only, so the standalone repo needs no protoAgent dependency),
    and a conftest that uses it to load the plugin as a package."""
    tdir = target / "tests"
    tdir.mkdir()
    # In the frozen desktop app this module lives in the PYZ with no source on disk —
    # the sidecar bundles testkit.py as data beside it (build_sidecar BUNDLED_DATA) so
    # this path resolves there too. Fail with the fix, not a bare FileNotFoundError.
    kit = Path(__file__).with_name("testkit.py")
    if not kit.is_file():
        raise FileNotFoundError(
            "plugin testkit source unavailable in this build — scaffold without "
            "with_tests, or update the desktop app (the sidecar must bundle "
            "graph/plugins/testkit.py)"
        )
    testkit_src = kit.read_text()
    (tdir / "_plugin_testkit.py").write_text(testkit_src)
    (tdir / "conftest.py").write_text(_CONFTEST_STUB.format(id=pid))
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
            id=bid,
            name=name,
            summary=summary,
            members=members_block,
            enabled=", ".join(enabled_ids),
        )
    )
    return Scaffolded(id=bid, path=target, made=["protoagent.bundle.yaml"], kind="bundle")
