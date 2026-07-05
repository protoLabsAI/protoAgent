"""The ``protoagent`` command — the terminal control plane for a protoAgent runtime.

ADR 0075 D1. This is the installable front door (``console_scripts`` →
``protoagent``) that replaces the boring ``python -m server <sub>`` invocation:
one discoverable command tree with ``--help``, covering install/manage
(``plugin`` / ``workspace`` / ``fleet`` / ``skills`` / ``config``) and lifecycle
(``up`` / ``down`` / ``status`` / ``serve`` / ``setup``).

Design: ``dispatch()`` is the *single* subcommand router, shared by BOTH
entrypoints — the ``protoagent`` script (:func:`main`) and ``python -m server``
(``server.__init__._main`` calls ``dispatch`` instead of its own inline
branches). So the two front doors can never drift. The management subcommands are
**re-parented, not rewritten**: they forward to the existing ``graph/**/cli.py``
dispatchers verbatim. Chat is deliberately absent — that's ``proto``'s job (the
A2A client); this CLI is a control plane.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Management subcommands re-parented from `python -m server <sub>` — name → the
# (module, callable) implementing it. Each is `run_*_cli(argv: list[str]) -> int`,
# acts on disk/DBs, and exits. Imported lazily so `protoagent --help` stays fast
# and a broken optional dep can't break the whole CLI.
_FORWARD: dict[str, tuple[str, str]] = {
    "plugin": ("graph.plugins.cli", "run_plugin_cli"),
    "workspace": ("graph.workspaces.cli", "run_workspace_cli"),
    "skills": ("graph.skills.cli", "run_skills_cli"),
    "fleet": ("graph.fleet.cli", "run_fleet_cli"),
    "config": ("graph.config_explain", "run_config_cli"),
    "model": ("graph.model_cli", "run_model_cli"),
    "operations": ("ops.cli", "run_operations_cli"),
    # `knowledge` lives in server/ (not graph/**) — it boots the instance's stores standalone.
    "knowledge": ("server.knowledge_cli", "run_knowledge_cli"),
}

_FORWARD_HELP = {
    "plugin": "Install / list / update / uninstall drop-in plugins (ADR 0027)",
    "workspace": "Create / list / run / remove isolated workspace agents (ADR 0041)",
    "skills": "Inspect and curate the SKILL.md library (ADR 0041)",
    "fleet": "Start / stop / list fleet MEMBER agents as background processes (ADR 0042)",
    "config": "Explain / get / set this instance's config (ADR 0047)",
    "model": "Point at a local / OpenAI-compatible LLM — Ollama, LM Studio, llama.cpp, vLLM (ADR 0075)",
    "operations": "List the operations on the ops layer — name, read/write, summary (ADR 0075)",
    "knowledge": "Ingest a URL / file into this instance's knowledge base (ADR 0075)",
}

_LIFECYCLE_HELP = {
    "up": "Start THIS instance's server in the background (detached)",
    "down": "Stop the background server started by `protoagent up`",
    "status": "Show whether this instance's server is running (port, pid, version)",
    "serve": "Run the server in the FOREGROUND (= `python -m server`)",
    "setup": "Complete headless setup for the live config (ADR 0010)",
}

_DEFAULT_PORT = 7870


# ── subcommand router (shared by both entrypoints) ───────────────────────────


def dispatch(argv: list[str]) -> int | None:
    """Route a management or lifecycle subcommand.

    Returns the subcommand's exit code when it handled ``argv``; returns ``None``
    when ``argv`` is NOT one of ours (a bare invocation / ``serve`` / ``setup`` /
    server flags), signalling the caller to boot the server instead. Never boots
    the server itself — that stays with the caller so ``python -m server`` keeps
    its exact existing behavior."""
    if not argv:
        return None
    cmd = argv[0]
    if cmd in _FORWARD:
        module, func = _FORWARD[cmd]
        run = getattr(importlib.import_module(module), func)
        return int(run(argv[1:]) or 0)
    if cmd == "up":
        return _cmd_up(argv[1:])
    if cmd == "down":
        return _cmd_down(argv[1:])
    if cmd == "status":
        return _cmd_status(argv[1:])
    return None


# ── lifecycle: this instance's server (default instance, not fleet members) ──


def _pid_path() -> Path:
    """The instance-scoped pidfile for a `protoagent up` server. Lives at the
    instance root so a scoped/dev instance tracks its own server separately."""
    from infra.paths import instance_paths

    return instance_paths().instance_root / "server.pid"


def _read_pidfile() -> dict | None:
    p = _pid_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """True if something is accepting connections on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _server_base_argv() -> list[str]:
    """The frozen-aware base argv for (re)launching the server (mirrors the fleet
    supervisor / workspace-runner pattern): the PyInstaller onefile re-invokes
    itself; a source checkout runs ``python -m server``."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "server"]


def _lifecycle_parser(cmd: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"protoagent {cmd}", description=_LIFECYCLE_HELP[cmd])
    p.add_argument("--port", type=int, default=_DEFAULT_PORT, help=f"server port (default {_DEFAULT_PORT})")
    if cmd == "up":
        p.add_argument("--host", type=str, default="", help="bind host (default: config / loopback)")
        p.add_argument("--wait", type=float, default=15.0, help="seconds to wait for the port to bind")
    return p


def _cmd_status(rest: list[str]) -> int:
    args = _lifecycle_parser("status").parse_args(rest)
    rec = _read_pidfile()
    port = int((rec or {}).get("port") or args.port)
    if _port_open(port):
        pid = (rec or {}).get("pid", "?")
        ver = (rec or {}).get("version", "?")
        print(f"protoagent: running on http://127.0.0.1:{port} (pid {pid}, v{ver})")
        return 0
    print("protoagent: stopped")
    return 3  # sysadmin convention: 3 = not running


def _cmd_up(rest: list[str]) -> int:
    from infra.paths import package_version

    args = _lifecycle_parser("up").parse_args(rest)
    if _port_open(args.port):
        print(f"protoagent: already running on http://127.0.0.1:{args.port}")
        return 0

    log_path = _pid_path().parent / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [*_server_base_argv(), "--port", str(args.port)]
    if args.host:
        argv += ["--host", args.host]

    # Detached: own session so it outlives this CLI process; output tees to the log.
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            argv, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, start_new_session=True
        )

    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # died during boot
            print(f"protoagent: server exited during boot (code {proc.returncode}) — see {log_path}", file=sys.stderr)
            return 1
        if _port_open(args.port):
            _pid_path().write_text(
                json.dumps({"pid": proc.pid, "port": args.port, "version": package_version()}),
                encoding="utf-8",
            )
            print(f"protoagent: started on http://127.0.0.1:{args.port} (pid {proc.pid})")
            return 0
        time.sleep(0.3)

    print(f"protoagent: started (pid {proc.pid}) but port {args.port} didn't bind in {args.wait:g}s — see {log_path}", file=sys.stderr)
    return 1


def _cmd_down(rest: list[str]) -> int:
    args = _lifecycle_parser("down").parse_args(rest)
    rec = _read_pidfile()
    pid = (rec or {}).get("pid")
    if not pid or not _pid_alive(int(pid)):
        # No pidfile / dead pid — but something may still hold the port (started by
        # hand). Be honest rather than kill a process we didn't launch.
        port = int((rec or {}).get("port") or args.port)
        _pid_path().unlink(missing_ok=True)
        if _port_open(port):
            print(f"protoagent: a server is on :{port} but wasn't started by `protoagent up` — stop it where it runs", file=sys.stderr)
            return 1
        print("protoagent: not running")
        return 0

    pid = int(pid)
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):  # up to ~8s for a graceful stop
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    else:
        os.kill(pid, signal.SIGKILL)
    _pid_path().unlink(missing_ok=True)
    print(f"protoagent: stopped (pid {pid})")
    return 0


# ── `protoagent` entrypoint (console_scripts) ────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """A help-only parser: it lists the command tree for ``protoagent --help``;
    actual routing is done by :func:`dispatch` (+ serve/setup) so forwarded
    subcommands keep their own arg parsing intact."""
    parser = argparse.ArgumentParser(
        prog="protoagent",
        description="protoAgent — run and manage a local AI agent runtime from the terminal.",
        epilog="Chatting with an agent is `proto`'s job (the A2A client); this command runs and manages the runtime.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name in ("serve", "up", "down", "status", "setup"):
        sub.add_parser(name, help=_LIFECYCLE_HELP[name], add_help=False)
    for name in _FORWARD:
        sub.add_parser(name, help=_FORWARD_HELP[name], add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    """``protoagent`` entrypoint. Returns a process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0

    code = dispatch(argv)
    if code is not None:
        return code

    cmd = argv[0]
    if cmd == "serve":
        return _boot_server(argv[1:])
    if cmd == "setup":
        return _boot_server(["--setup", *argv[1:]])

    parser.print_help(sys.stderr)
    print(f"\nprotoagent: unknown command {cmd!r}", file=sys.stderr)
    return 2


def _boot_server(server_argv: list[str]) -> int:
    """Hand off to the server boot (``server._main``) with a normalized argv. Used
    for ``serve`` (foreground) and ``setup`` (the ``--setup`` one-shot). ``_main``
    may ``raise SystemExit`` (setup / uvicorn) — that propagates as the exit code."""
    import server

    sys.argv = ["protoagent", *server_argv]
    server._main()
    return 0


if __name__ == "__main__":  # `python -m server.cli`
    raise SystemExit(main())
