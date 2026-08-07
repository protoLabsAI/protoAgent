"""Plugin Devkit — the plugin-authoring kit + the reference plugin (ADR 0027, ADR 0096).

The featured full-bundle example: in ONE plugin it contributes **tools**
(`scaffold_plugin`, `scaffold_bundle`, `enable_plugin`, `reload_plugins`,
`plugin_list_files`, `plugin_read_file`, `plugin_write_file`, `test_plugin`), a
**subagent** (`plugin-architect`), a bundled **skill** (`skills/building-plugins`),
a **workflow** (`workflows/design-plugin`), a **console view** (`/guide`), and
**config/settings** — every contribution type.

Enable it to let the agent build its own plugins *and test them live* — the ADR 0096
self-building loop: scaffold → edit (`plugin_write_file`) → test (`test_plugin`) →
hot-swap (`reload_plugins`), no restart. The scaffolders themselves live in core
(`graph.plugins.scaffold`) so the `plugin new` CLI shares them; this file is the
agent-facing half + the live-enable that needs the running graph.
"""

from __future__ import annotations

import asyncio
import os
import re as _re
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

from graph.plugins import scaffold
from graph.subagents.config import SubagentConfig

# Captured at register() so the scaffold tools can broadcast on the event bus (ADR 0039) —
# the devkit dogfoods its own lesson.
_REGISTRY = None

# test_plugin bounds — a runaway suite must not wedge a turn.
_TEST_TIMEOUT_S = 180
_TEST_OUTPUT_CAP = 6_000
# plugin_read_file / plugin_write_file bounds.
_READ_CAP = 48_000
_WRITE_CAP = 262_144
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}
_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _emit_scaffolded(pid: str, kind: str) -> None:
    try:
        if _REGISTRY is not None:
            _REGISTRY.emit("scaffolded", {"id": pid, "kind": kind})  # → "plugin-devkit.scaffolded"
    except Exception:  # noqa: BLE001 — a bus hiccup must never fail the scaffold
        pass


def _plugin_meta(pid: str) -> dict | None:
    """The latest load meta for a plugin (``{id, loaded, error, tools, …}``) off the
    runtime — set by the loader on every (re)load. ``None`` if the loader didn't see it."""
    from runtime.state import STATE

    return next((m for m in (getattr(STATE, "plugin_meta", []) or []) if m.get("id") == pid), None)


def _failure_detail(meta: dict) -> str:
    """The agent-facing failure text: the error line PLUS the loader's bounded traceback
    (ADR 0096 D4) — ``name 'foo' is not defined`` with no location is not fixable."""
    err = meta.get("error") or "unknown error"
    tb = (meta.get("traceback") or "").strip()
    if tb and tb not in err:
        err = f"{err}\n{tb}"
    return err


# ── the devkit's own fence (ADR 0096 D2) ─────────────────────────────────────
# The plugins dir is the devkit's domain: file tools resolve ONLY under the live
# plugins dir (or the configured target_dir). The operator fs fence (ADR 0007) is
# deliberately NOT reused or widened — enabling a build tool must not change what
# the operator granted elsewhere.


def _devkit_roots(target_dir: str | None) -> list[Path]:
    roots: list[Path] = []
    if target_dir:
        roots.append(Path(target_dir).expanduser().resolve())
    try:
        roots.append(scaffold.live_plugins_dir().resolve())
    except Exception:  # noqa: BLE001 — a broken instance path just narrows the fence
        pass
    return roots


def _plugin_dir(pid: str, target_dir: str | None) -> Path:
    """Resolve a plugin id to its on-disk dir under the devkit's roots, or raise
    ``ValueError`` with an agent-facing message. Only real plugin dirs (a
    ``protoagent.plugin.yaml`` present) are addressable — the fence is by id, not path."""
    if not _ID_RE.match(pid or ""):
        raise ValueError(f"invalid plugin id {pid!r} — ids are lowercase slugs like 'my-plugin'")
    for root in _devkit_roots(target_dir):
        cand = (root / pid).resolve()
        if cand.parent != root:  # a crafted id must not resolve outside the root
            continue
        if (cand / "protoagent.plugin.yaml").is_file():
            return cand
    raise ValueError(f"no plugin {pid!r} in the plugins dir — scaffold_plugin creates one, or check the id")


def _resolve_member(pdir: Path, rel: str) -> Path:
    """A path INSIDE the plugin dir: relative only, no ``..``, symlink escapes refused."""
    p = Path(rel or "")
    if not rel or p.is_absolute() or any(part == ".." for part in p.parts):
        raise ValueError(f"path must be relative, inside the plugin dir (got {rel!r})")
    out = (pdir / p).resolve()
    if out != pdir and pdir not in out.parents:
        raise ValueError(f"path escapes the plugin dir: {rel!r}")
    return out


def _live_enable(pid: str) -> tuple[bool, str]:
    """Enable a plugin in the RUNNING agent + hot-reload — the same path the console
    enable toggle uses (#822): tools/subagents/middleware/MCP rebuild with the graph
    and the plugin's router hot-mounts, so a freshly enabled plugin is live with no
    restart. No-ops cleanly when there's no live graph (the CLI / tests).

    Crucially it confirms the plugin actually LOADED — ``load_plugins`` is best-effort
    per-plugin, so a ``register()`` that raises is *skipped*, not fatal: the config
    reload "succeeds" while the plugin is silently not live. We surface that instead of
    a false 'loaded live', so the agent can fix-and-reload instead of testing a no-op.

    Blocking by design (the reload's graph recompile is heavy) — async tool bodies call
    it via ``asyncio.to_thread`` (ADR 0096 D9), never inline.
    """
    try:
        from runtime.state import STATE

        if getattr(STATE, "graph", None) is None:
            return (False, "not running — enable it when the agent is live")
        cfg = STATE.graph_config
        enabled = [p for p in (getattr(cfg, "plugins_enabled", []) or []) if p != pid]
        enabled.append(pid)
        disabled = [p for p in (getattr(cfg, "plugins_disabled", []) or []) if p != pid]
        from server.agent_init import _apply_settings_changes

        ok, msgs = _apply_settings_changes(config={"plugins": {"enabled": enabled, "disabled": disabled}})
        if not ok:
            return (False, "; ".join(msgs) or "reload failed")
        meta = _plugin_meta(pid)
        if meta is None:
            return (
                False,
                "enabled in config, but the loader didn't discover it — check the id and that it's in the plugins dir",
            )
        if not meta.get("loaded"):
            return (
                False,
                f"enabled, but it FAILED to load: {_failure_detail(meta)}\n"
                f"— fix it (plugin_read_file / plugin_write_file), then call reload_plugins",
            )
        return (True, "enabled + loaded live")
    except Exception as e:  # noqa: BLE001 — enable is best-effort; the skeleton still landed
        return (False, f"auto-enable failed: {e}")


def _build_scaffold_tool(config: dict | None):
    """Closes over the plugin's config so the tool knows where to write."""
    target_dir = (config or {}).get("target_dir") or None

    @tool
    async def scaffold_plugin(
        name: str,
        summary: str = "A protoAgent plugin.",
        with_tool: bool = True,
        with_view: bool = False,
        with_skill: bool = False,
        with_workflow: bool = False,
        with_comms: bool = False,
        with_tests: bool = False,
        git_init: bool = False,
        enable: bool = True,
    ) -> str:
        """Scaffold a new protoAgent plugin SKELETON on disk AND enable it live —
        so you can build a plugin and test it in the SAME session, no restart.

        Writes the manifest + ``register()`` + optional view/skill/workflow stubs,
        then (``enable=True``, the default) turns it on and hot-reloads the agent:
        its tools/views are live on your NEXT turn. Iterate with ``plugin_write_file``
        + ``reload_plugins``; verify with ``test_plugin``.

        Set ``with_comms=True`` for a **communication plugin** (Discord/Slack/Telegram-
        style): it writes a ``ChatAdapter`` skeleton (ADR 0029) — you fill in
        connect/receive/send, then enable it from Settings (it needs a token), so
        comms plugins are NOT auto-enabled here.

        Set ``with_tests=True`` to also write a host-free test suite + CI +
        requirements-dev + pyproject — use it when scaffolding a STANDALONE-repo
        plugin (its own git repo) so it's shippable + green from birth; skip it for a
        plugin bundled inside protoAgent (which rides the host's tests/CI). Pair it
        with ``git_init=True`` for a repo from birth (init + initial commit) when the
        plugin should outlive this session or graduate to its own remote.

        Use this when asked to create/build/scaffold a plugin; see the building-plugins
        skill for the contract.
        """
        try:
            res = await asyncio.to_thread(
                scaffold.scaffold_plugin,
                name,
                summary=summary,
                with_tool=with_tool,
                with_view=with_view,
                with_skill=with_skill,
                with_workflow=with_workflow,
                with_comms=with_comms,
                with_tests=with_tests,
                git_init=git_init,
                target_dir=target_dir,
            )
        except FileExistsError as e:
            return f"✗ {scaffold.slug(name)!r} already exists at {e} — pick another name or remove it first."

        _emit_scaffolded(res.id, res.kind)
        kind_label = "communication plugin" if res.kind == "comms" else res.kind
        lines = [f"✓ scaffolded {kind_label} {res.id!r} at {res.path}", f"  wrote: {', '.join(res.made)}"]

        if res.kind == "comms":
            cls = scaffold._class_name(name, res.id.replace("-", "_"))
            lines.append(
                f"  next: implement {cls}Adapter.run/validate (see plugins/telegram), then enable it\n"
                f"        from Settings (it needs a bot token). Guide: docs/guides/communication-plugins.md"
            )
            return "\n".join(lines)

        if enable:
            # Heavy graph recompile — off the event loop, explicitly (ADR 0096 D9).
            ok, detail = await asyncio.to_thread(_live_enable, res.id)
            if ok:
                hello = f"{res.id.replace('-', '_')}_hello" if with_tool else "its tools"
                lines.append(f"  ✓ {detail} — call {hello} on your NEXT turn to test it (no restart).")
                lines.append(
                    f"  iterate: plugin_write_file({res.id!r}, …) then reload_plugins; test_plugin({res.id!r}) runs its suite."
                )
            else:
                lines.append(f"  ⚠ scaffolded but not auto-enabled ({detail}) — call enable_plugin({res.id!r}).")
        else:
            lines.append(f"  next: fill in the logic, then call enable_plugin({res.id!r}) to load it live.")
        lines.append("  (see the building-plugins skill for the full contract)")
        return "\n".join(lines)

    return scaffold_plugin


def _build_scaffold_bundle_tool(config: dict | None):
    target_dir = (config or {}).get("target_dir") or None

    @tool
    def scaffold_bundle(
        name: str,
        summary: str = "A protoAgent plugin bundle.",
        members: list[dict] | None = None,
        enabled: list[str] | None = None,
    ) -> str:
        """Scaffold a plugin BUNDLE (ADR 0040) — a ``protoagent.bundle.yaml`` that
        names a set of plugins to install + enable together (like the PM stack).

        ``members`` is a list of ``{id, url, ref}`` (a git plugin) or
        ``{id, builtin: true}`` (one that ships with protoAgent); omit it for a
        REPLACE_ME template. ``enabled`` defaults to every member.

        A bundle is a reference manifest (no code) — it's not enabled live like a
        plugin. Commit/push it, then install the whole stack with
        ``plugin install <bundle-repo-url>``.
        """
        try:
            res = scaffold.scaffold_bundle(
                name,
                summary=summary,
                members=members,
                enabled=enabled,
                target_dir=target_dir,
            )
        except FileExistsError as e:
            return f"✗ {scaffold.slug(name)!r} already exists at {e} — pick another name or remove it first."
        _emit_scaffolded(res.id, res.kind)
        return (
            f"✓ scaffolded bundle {res.id!r} at {res.path}\n"
            f"  wrote: {', '.join(res.made)}\n"
            f"  next: fill in the member plugins (git url + ref, or builtin: true), then commit/push it\n"
            f"        and install the stack: `plugin install <this-repo-url>` (ADR 0040)."
        )

    return scaffold_bundle


# ── the edit half of the loop (ADR 0096 D2) ──────────────────────────────────


def _build_file_tools(config: dict | None) -> list:
    target_dir = (config or {}).get("target_dir") or None

    @tool
    def plugin_list_files(plugin_id: str) -> str:
        """List the files of a plugin in the plugins dir (one you scaffolded or
        installed) — relative paths + sizes. Use before reading/editing it."""
        try:
            pdir = _plugin_dir(plugin_id, target_dir)
        except ValueError as e:
            return f"✗ {e}"
        rows = []
        for p in sorted(pdir.rglob("*")):
            if any(part in _SKIP_DIRS for part in p.relative_to(pdir).parts):
                continue
            if p.is_file():
                rows.append(f"{p.relative_to(pdir)}  ({p.stat().st_size} B)")
            if len(rows) >= 200:
                rows.append("… (truncated at 200 files)")
                break
        return f"{pdir}:\n" + "\n".join(rows)

    @tool
    def plugin_read_file(plugin_id: str, path: str) -> str:
        """Read a file inside a plugin's dir (relative path, e.g. ``__init__.py``).
        This is how you inspect a plugin you're iterating on — pair with
        ``plugin_write_file`` + ``reload_plugins``."""
        try:
            pdir = _plugin_dir(plugin_id, target_dir)
            target = _resolve_member(pdir, path)
        except ValueError as e:
            return f"✗ {e}"
        if not target.is_file():
            return f"✗ no file {path!r} in {plugin_id!r} — plugin_list_files shows what's there"
        text = target.read_text(errors="replace")
        if len(text) > _READ_CAP:
            return text[:_READ_CAP] + f"\n… ✂ truncated ({len(text)} chars total)"
        return text

    @tool
    def plugin_write_file(plugin_id: str, path: str, content: str) -> str:
        """Write/overwrite a file inside a plugin's dir (relative path). The edit half
        of the build loop: write, then ``reload_plugins`` to go live, ``test_plugin``
        to verify. Fenced to the plugins dir — this cannot touch anything else."""
        try:
            pdir = _plugin_dir(plugin_id, target_dir)
            target = _resolve_member(pdir, path)
        except ValueError as e:
            return f"✗ {e}"
        if len(content) > _WRITE_CAP:
            return f"✗ content too large ({len(content)} chars > {_WRITE_CAP}) — split the file"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return (
            f"✓ wrote {path} ({len(content)} chars) — call reload_plugins to make it live, "
            f"test_plugin({plugin_id!r}) to run its suite"
        )

    return [plugin_list_files, plugin_read_file, plugin_write_file]


# ── the test half of the loop (ADR 0096 D3) ──────────────────────────────────


def _test_interpreter() -> tuple[str | None, str | None]:
    """``(python, error)`` — the interpreter to run pytest with. Source-run →
    ``sys.executable``; frozen → the ADR 0094 managed runtime or an actionable
    refusal (never a system-Python fallback)."""
    if getattr(sys, "frozen", False):
        try:
            from infra.python_runtime import managed_python_exe
        except Exception:  # noqa: BLE001 — ancient build without the runtime module
            return None, "no Python available to run pytest in this packaged build"
        exe = managed_python_exe()
        if exe is None:
            return None, (
                "no Python to run pytest in the packaged app — install the managed runtime "
                "(Settings ▸ Tools, ~35 MB), then try again"
            )
        return str(exe), None
    return sys.executable, None


def _run_pytest(pdir: Path) -> str:
    py, err = _test_interpreter()
    if py is None:
        return f"✗ {err}"
    # Scrubbed env (the execute_code posture): the suite is host-free by design and
    # must not inherit gateway keys/tokens from the server process.
    env = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SYSTEMROOT")
        if k in os.environ
    }
    env["PYTHONNOUSERSITE"] = "1"
    try:
        proc = subprocess.run(
            [py, "-m", "pytest", "-q", "--color=no"],
            cwd=pdir,
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"✗ tests timed out after {_TEST_TIMEOUT_S}s — is something blocking on network/input?"
    out = (proc.stdout + "\n" + proc.stderr).strip()
    if len(out) > _TEST_OUTPUT_CAP:
        out = "… ✂ …\n" + out[-_TEST_OUTPUT_CAP:]
    if proc.returncode == 0:
        return f"✓ tests passed\n{out}"
    if proc.returncode == 5:
        return (
            "✗ no tests collected — write tests/test_*.py (scaffold_plugin with_tests=True generates a host-free suite)"
        )
    if "No module named pytest" in out:
        hint = (
            "pytest isn't installed in the managed runtime — install the plugin's dev deps first"
            if getattr(sys, "frozen", False)
            else f"pytest isn't installed in this environment ({py})"
        )
        return f"✗ {hint}"
    return f"✗ tests failed (exit {proc.returncode})\n{out}"


def _build_test_tool(config: dict | None):
    target_dir = (config or {}).get("target_dir") or None

    @tool
    async def test_plugin(plugin_id: str) -> str:
        """Run a plugin's own pytest suite (``tests/``, host-free via the vendored
        testkit) in a subprocess and report pass/fail + the output tail. The verify
        step of the build loop: scaffold → edit → **test** → reload. Bounded by a
        timeout and an output cap."""
        try:
            pdir = _plugin_dir(plugin_id, target_dir)
        except ValueError as e:
            return f"✗ {e}"
        return await asyncio.to_thread(_run_pytest, pdir)

    return test_plugin


# ── the delegated lane (ADR 0096 D5) ─────────────────────────────────────────
# A substantial build is handed to a configured ACP coding delegate (ADR 0025)
# working directly in the plugin dir, then re-joins the spine at *test*. The
# delegates plugin is `builtin: true` (core runtime infrastructure, always
# loaded), so importing its registry here follows the projectBoard precedent —
# it is the one plugin that is really a core surface, not a peer.

_DEVELOP_TIMEOUT_S = 900
_CODER_REPLY_CAP = 2_000

_DEVELOP_PROMPT = """You are implementing a protoAgent plugin. Your working directory IS the plugin
directory — edit ONLY files inside it. The contract: `protoagent.plugin.yaml` is the
manifest (data, read without importing); `__init__.py` exposes `register(registry)`
(registry.register_tool / register_subagent / register_router / emit); `skills/` and
`workflows/` are auto-discovered data. Keep it the smallest change that satisfies the
task. Write or update tests under `tests/` when the plugin has a suite. Do NOT run
git, do NOT create commits or PRs, do NOT touch anything outside this directory —
the host runs the tests and reloads the plugin after you finish.

Task:
{instructions}"""


def _resolve_coder(config: dict | None):
    """``(delegate, error)`` — the ACP coding delegate develop_plugin dispatches to.
    ``plugin_devkit.coder`` names one; otherwise the sole configured acp delegate
    wins; anything else is an actionable error, mirroring the delegates plugin's own
    degrade posture (no roster → say what to configure, don't guess)."""
    try:
        from plugins.delegates import _load_delegates_config
        from plugins.delegates.registry import DelegateRegistry

        reg = DelegateRegistry(_load_delegates_config())
    except Exception as e:  # noqa: BLE001 — a broken roster is an answer, not a crash
        return None, f"delegate registry unavailable: {e}"
    want = (config or {}).get("coder") or ""
    if want:
        d = reg.get(want)
        if d is None:
            return None, f"no delegate named {want!r} (plugin_devkit.coder) — check list_agents"
        if d.type != "acp":
            return None, f"{want!r} is type {d.type!r} — develop_plugin needs an `acp` coding delegate"
        return d, None
    acp = [d for d in (reg.get(n) for n in reg.names()) if d is not None and d.type == "acp"]
    if len(acp) == 1:
        return acp[0], None
    if not acp:
        return None, (
            "no `acp` coding delegate configured — add one under `delegates:` "
            "(docs/guides/coding-agents.md) or set plugin_devkit.coder"
        )
    return None, f"multiple acp delegates ({', '.join(d.name for d in acp)}) — set plugin_devkit.coder to pick one"


def _build_develop_tool(config: dict | None):
    target_dir = (config or {}).get("target_dir") or None

    @tool
    async def develop_plugin(plugin_id: str, instructions: str) -> str:
        """Hand a plugin's implementation to the configured ACP coding delegate — the
        heavy-build lane of the loop. The coder works directly in the plugin dir
        (scoped: fresh session, git lifecycle off), then the host automatically runs
        ``test_plugin`` and ``reload_plugins`` and reports all three results.

        Use for substantial changes (multi-file logic, a real feature); for small
        edits prefer ``plugin_write_file`` yourself. Requires an ``acp`` delegate
        (docs/guides/coding-agents.md); ``plugin_devkit.coder`` picks one when
        several are configured."""
        import dataclasses

        try:
            pdir = _plugin_dir(plugin_id, target_dir)
        except ValueError as e:
            return f"✗ {e}"
        if not (instructions or "").strip():
            return "✗ empty instructions — say what to build/change"
        coder, err = _resolve_coder(config)
        if coder is None:
            return f"✗ {err}"

        from plugins.delegates.adapters import ADAPTERS, DelegateError

        adapter = ADAPTERS["acp"]
        # The projectBoard dispatch pattern: a per-call scoped copy (registry
        # untouched) with the plugin dir as workdir; manage_git force-off — the
        # devkit owns the lifecycle here (test + reload), a PR would be noise.
        overrides: dict = {"workdir": str(pdir)}
        if any(f.name == "manage_git" for f in dataclasses.fields(coder)):
            overrides["manage_git"] = False
        scoped = dataclasses.replace(coder, **overrides)
        timeout = float(getattr(coder, "timeout_s", 0) or _DEVELOP_TIMEOUT_S)
        prompt = _DEVELOP_PROMPT.format(instructions=instructions.strip())
        try:
            await adapter.forget_session(scoped)  # fresh session — no stale memory of prior runs
        except Exception:  # noqa: BLE001 — best-effort; a stale session must not block the build
            pass
        try:
            reply = await asyncio.wait_for(adapter.dispatch(scoped, prompt, timeout=timeout), timeout)
        except asyncio.TimeoutError:
            return f"✗ coder timed out after {int(timeout)}s — split the task or raise the delegate's timeout_s"
        except DelegateError as e:
            return f"✗ coder dispatch failed: {e}"
        finally:
            try:
                await adapter.teardown(scoped)  # reap the workdir-scoped subprocess
            except Exception:  # noqa: BLE001 — never let teardown mask the result
                pass

        reply = (reply or "").strip()
        if len(reply) > _CODER_REPLY_CAP:
            reply = reply[:_CODER_REPLY_CAP] + " … ✂"
        lines = [f"✓ coder ({coder.name}) finished on {plugin_id!r}:", reply or "(no reply text)", ""]

        # Re-join the spine at *test* (D5): verify, then hot-swap.
        lines.append("— test_plugin —")
        lines.append(await asyncio.to_thread(_run_pytest, pdir))

        from runtime.state import STATE

        if getattr(STATE, "graph", None) is None:
            lines.append("— reload skipped (no live agent) — call enable_plugin when running")
            return "\n".join(lines)
        meta = _plugin_meta(plugin_id)
        if meta is not None and meta.get("enabled"):
            from server.agent_init import _apply_settings_changes

            ok, msgs = await asyncio.to_thread(_apply_settings_changes)  # D9: off the loop
            fresh = _plugin_meta(plugin_id)
            if not ok:
                lines.append(f"— reload failed: {'; '.join(msgs)}")
            elif fresh is not None and not fresh.get("loaded"):
                lines.append(f"— reloaded, but it FAILED to load: {_failure_detail(fresh)}")
            else:
                lines.append("— reloaded: the coder's changes are live on the next turn")
        else:
            lines.append(f"— not enabled yet: call enable_plugin({plugin_id!r}) to load it live")
        return "\n".join(lines)

    return develop_plugin


# ── graduation (ADR 0096 D6) ─────────────────────────────────────────────────
# The ADR 0095 managed-projects registry's first runtime write path — scoped to
# the plugins dir ONLY. A registry entry is also an fs-fence grant (ADR 0095 D3),
# so the agent must never widen its own fence outside the domain it already owns;
# general project registration stays an operator/console action.


def _current_branch(pdir) -> str:
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=pdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — identity garnish, never load-bearing
        return ""


def _build_register_project_tool(config: dict | None):
    target_dir = (config or {}).get("target_dir") or None

    @tool
    async def register_plugin_project(plugin_id: str, github: str = "") -> str:
        """Register a plugin's dir as a managed project (ADR 0095) — the graduation
        step for a plugin that outgrows the in-place loop. Once registered it's a
        first-class project: the fs tools address it by name, the github plugin's
        repo picker sees ``github``, and a projectBoard instance can target it via
        ``project_board.project``.

        Typical graduation: ``scaffold_plugin(..., git_init=True)`` → create + push a
        remote → ``register_plugin_project(id, github="owner/repo")`` → point a board
        at it. Scoped to the plugins dir — this cannot register arbitrary paths."""
        try:
            pdir = _plugin_dir(plugin_id, target_dir)
        except ValueError as e:
            return f"✗ {e}"
        if github and not _re.match(r"^[\w.-]+/[\w.-]+$", github):
            return f"✗ github must be owner/repo (got {github!r})"
        from runtime.state import STATE

        if getattr(STATE, "graph", None) is None:
            return "✗ not running — register it when the agent is live"
        projects = [dict(p) for p in (getattr(STATE.graph_config, "projects", None) or []) if isinstance(p, dict)]
        for p in projects:
            if p.get("name") == plugin_id or p.get("path") == str(pdir):
                return f"✓ already registered as {p.get('name')!r} — nothing to do"
        entry: dict = {"name": plugin_id, "path": str(pdir), "write": True}
        if github:
            entry["github"] = github
        if (pdir / ".git").is_dir():
            branch = _current_branch(pdir)
            if branch:
                entry["default_branch"] = branch
        from server.agent_init import _apply_settings_changes

        # Heavy graph recompile — off the event loop, explicitly (ADR 0096 D9).
        ok, msgs = await asyncio.to_thread(_apply_settings_changes, config={"projects": [*projects, entry]})
        if not ok:
            return f"✗ register failed: {'; '.join(msgs)}"
        lines = [
            f"✓ registered {plugin_id!r} as a managed project ({entry['path']})",
            "  fs tools now address it by name; a projectBoard can target it via project_board.project",
        ]
        if github:
            lines.append(f"  github: {github} (the github plugin's repo picker sees it)")
        elif not (pdir / ".git").is_dir():
            lines.append("  tip: scaffold_plugin(..., git_init=True) next time — a repo graduates cleaner")
        return "\n".join(lines)

    return register_plugin_project


@tool
async def enable_plugin(plugin_id: str) -> str:
    """Enable an already-present plugin (one you scaffolded or installed) by id and
    hot-reload it live — no restart. Use when a plugin is on disk but turned off."""
    # Heavy graph recompile — off the event loop, explicitly (ADR 0096 D9).
    ok, detail = await asyncio.to_thread(_live_enable, plugin_id)
    return f"✓ {plugin_id}: {detail}" if ok else f"✗ {plugin_id}: {detail}"


@tool
async def reload_plugins() -> str:
    """Hot-reload all enabled plugins — re-exec their code (every file, not just
    ``__init__.py``) so edits you made take effect WITHOUT a restart. Use after editing
    a plugin you're iterating on; the new tools/views are live on your NEXT turn.

    Reports which ENABLED plugins FAILED to load (a syntax error, a bad import — with
    the traceback) so you fix-and-reload instead of testing stale/no-op code — a failed
    plugin is skipped, never fatal."""
    try:
        from runtime.state import STATE

        if getattr(STATE, "graph", None) is None:
            return "✗ no live agent to reload (run inside the server)."
        from server.agent_init import _apply_settings_changes

        # Bare call = pure reload (picks up file edits). Heavy — off the event loop (D9).
        ok, msgs = await asyncio.to_thread(_apply_settings_changes)
        if not ok:
            return f"✗ reload failed: {'; '.join(msgs)}"
        failed = [
            (m["id"], _failure_detail(m))
            for m in (getattr(STATE, "plugin_meta", []) or [])
            # enabled-only: a DISABLED plugin never loads by design — reporting it as a
            # failure buried real breakage in ~a dozen lines of "unknown error" noise.
            if m.get("enabled") and not m.get("loaded")
        ]
        if failed:
            detail = "\n".join(f"{pid}: {err}" for pid, err in failed)
            return f"⚠ reloaded, but these plugins FAILED to load — fix the code and reload again:\n{detail}"
        return "✓ reloaded — your plugin edits are live on the next turn."
    except Exception as e:  # noqa: BLE001
        return f"✗ reload failed: {e}"


def _plugin_architect() -> SubagentConfig:
    """A text-only subagent that turns a plain-English request into a concrete
    plugin spec. Used by the design-plugin workflow."""
    return SubagentConfig(
        name="plugin-architect",
        description=(
            "Designs a protoAgent plugin from a plain-English request — picks the "
            "contribution types, drafts a complete protoagent.plugin.yaml, and "
            "sketches register(). Use before scaffolding a non-trivial plugin."
        ),
        system_prompt=(
            "You design protoAgent plugins. Given a request, output: (1) the plugin "
            "id + name, (2) which contributions it needs (tools / subagents / "
            "SKILL.md skills / workflows / console views / config+secrets / event-bus "
            "emits+subscribes), (3) a complete `protoagent.plugin.yaml`, and (4) a "
            "`register(registry)` sketch. Follow the plugin contract: the manifest is "
            "data; code runs only on enable; config_section is a string; skills/ and "
            "workflows/ subdirs auto-load; declare requires_pip, don't assume it's "
            "installed; console views are sandboxed iframes — serve the PAGE on the public "
            "/plugins/<id> prefix (an iframe load can't carry a bearer) and its DATA on the "
            "gated /api/plugins/<id> (ADR 0026/0038); plugins coordinate via the event bus "
            "(registry.emit/on), never "
            "by importing each other (ADR 0039). Keep it to the smallest plugin that "
            "satisfies the request."
        ),
        tools=[],  # pure reasoning — it produces a spec, it doesn't act
    )


def _build_guide_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    # This page is itself a four-rules-compliant view (the reference plugin should
    # model the contract): it derives a slug-aware BASE, links the DS plugin-kit off
    # it (so it themes live via --pl-* tokens), and serves NO bearer-gated data —
    # it's a static card, so it lives on the PUBLIC /plugins/<id> prefix.
    @router.get("/guide")
    async def _guide():
        html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script>
          // RULE 3 — slug-aware base ("" on host, "/agents/<slug>" through the fleet proxy).
          var BASE = location.pathname.split("/plugins/")[0];
          // RULE 4 — link the DS kit CSS off BASE so the card themes live (no hardcoded hex).
          (function(){ var l=document.createElement("link"); l.rel="stylesheet";
            l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
        </script>
        <style>
          html,body{margin:0;background:var(--pl-color-bg);color:var(--pl-color-fg);
            font-family:var(--pl-font-sans, ui-sans-serif,system-ui,sans-serif)}
          .wrap{max-width:54ch;margin:0 auto;padding:40px 28px;line-height:1.6}
          h1{color:var(--pl-color-accent);font-size:22px;margin:0 0 4px}
          h2{color:var(--pl-color-accent);font-size:15px;margin:22px 0 6px}
          code{background:var(--pl-color-bg-raised);color:var(--pl-color-accent);
            padding:2px 6px;border-radius:var(--pl-radius,5px);font-size:13px}
          p,li{color:var(--pl-color-fg-muted);font-size:14px} ul{padding-left:18px}
        </style></head><body><div class="wrap">
          <h1>Plugin Devkit</h1>
          <p>This plugin gives the agent what it needs to build plugins — and is itself
          the full-bundle example. Ask the agent: <em>"build a plugin that …"</em>.</p>
          <h2>The build loop (no restart — ADR 0096)</h2>
          <ul>
            <li><code>scaffold_plugin</code> — writes a skeleton <strong>and enables it</strong>; its
                tools/view are live on the next turn (pass <code>with_tests</code> for a shippable repo)</li>
            <li><code>plugin_list_files</code> / <code>plugin_read_file</code> /
                <code>plugin_write_file</code> — inspect + edit it (fenced to the plugins dir)</li>
            <li><code>test_plugin</code> — run its pytest suite in a subprocess, pass/fail + output</li>
            <li><code>reload_plugins</code> — re-exec every plugin file; edits go live next turn
                (a load failure reports WITH its traceback)</li>
            <li><code>enable_plugin</code> — turn on a plugin that's on disk but off</li>
            <li><code>scaffold_bundle</code> — a <code>protoagent.bundle.yaml</code> stack (ADR 0040)</li>
          </ul>
          <h2>Also contributes</h2>
          <ul>
            <li><code>plugin-architect</code> subagent + <code>design-plugin</code> workflow — request → spec</li>
            <li>the <code>building-plugins</code> skill — the authoring contract</li>
            <li>this console view + config/settings; emits <code>plugin-devkit.scaffolded</code> (ADR 0039)</li>
          </ul>
          <h2>From the CLI</h2>
          <ul>
            <li><code>python -m server plugin new "My Plugin" --view --skill</code> — scaffold from the shell</li>
            <li><code>python -m server plugin new-bundle "My Stack" --member id=url@ref --builtin delegates</code></li>
          </ul>
          <h2>The plugin contract</h2>
          <ul>
            <li><code>protoagent.plugin.yaml</code> — manifest (data; read without importing)</li>
            <li><code>__init__.py</code> — <code>register(registry)</code> (tools, subagents, routes, MCP)</li>
            <li><code>skills/</code> + <code>workflows/</code> — auto-discovered data</li>
            <li><code>views:</code> — a rail icon → a <strong>sandboxed iframe</strong> of a page your plugin
                serves (ADR 0038). Serve the <strong>page</strong> on the public <code>/plugins/&lt;id&gt;</code>
                prefix (an iframe load can't carry a bearer); its <strong>data</strong> calls ride the
                gated <code>/api/plugins/&lt;id&gt;</code> via the DS kit's <code>apiFetch</code> (ADR 0026).</li>
          </ul>
          <h2>Events (ADR 0039)</h2>
          <ul>
            <li><code>registry.emit("x", data)</code> → <code>&lt;id&gt;.x</code> on the bus · <code>registry.on("other.*", fn)</code> to react</li>
            <li>plugins coordinate <em>only</em> via the bus — never import each other</li>
          </ul>
          <p>Fork components (compiled in, not sandboxed) use the build-time <code>src/ext</code> seam (ADR 0038).</p>
          <p>Full guides: <code>/guides/plugins</code> · <code>/guides/plugin-registry</code> · install ≠ enable ≠ trust.</p>
        </div></body></html>"""
        return HTMLResponse(html)

    return router


def register(registry) -> None:
    """Every contribution type, in one plugin (the point of the devkit)."""
    global _REGISTRY
    _REGISTRY = registry  # for the bus emit (ADR 0039)
    registry.register_tool(_build_scaffold_tool(registry.config))  # scaffold a plugin (+ enable live)
    registry.register_tool(_build_scaffold_bundle_tool(registry.config))  # scaffold a bundle
    for t in _build_file_tools(registry.config):  # inspect + edit the plugin (ADR 0096 D2)
        registry.register_tool(t)
    registry.register_tool(_build_test_tool(registry.config))  # run its pytest suite (ADR 0096 D3)
    registry.register_tool(_build_develop_tool(registry.config))  # hand a build to the ACP coder (ADR 0096 D5)
    registry.register_tool(_build_register_project_tool(registry.config))  # graduate to a managed project (ADR 0096 D6)
    registry.register_tool(enable_plugin)  # turn on an on-disk plugin live
    registry.register_tool(reload_plugins)  # pick up edits live
    registry.register_subagent(_plugin_architect())  # a subagent
    # PUBLIC /plugins/plugin-devkit (ADR 0026) — the console iframes /guide, and an
    # iframe page-load can't carry a bearer, so the page route must NOT be gated.
    registry.register_router(_build_guide_router(), prefix="/plugins/plugin-devkit")
    # skills/ + workflows/ auto-discover — no call needed (ADR 0027).
