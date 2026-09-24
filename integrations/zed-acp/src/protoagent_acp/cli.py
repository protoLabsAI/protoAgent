"""``protoagent-acp`` — the stdio entrypoint an ACP client (Zed) launches.

stdout is the ACP JSON-RPC channel: nothing else may ever be printed to it. Logs go to
stderr (Zed shows them under ``dev: open acp logs``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__, credentials


def _parse_root(value: str) -> tuple[str, str]:
    name, sep, path = value.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"expected NAME=/abs/path, got {value!r}")
    return name, path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protoagent-acp",
        description="Serve a running protoAgent instance as an ACP agent over stdio (for Zed's Agent Panel).",
    )
    p.add_argument("command", nargs="?", choices=["serve", "login"], default="serve",
                   help="serve (default): speak ACP on stdio · login: store a URL + token interactively")
    p.add_argument("--url", help="instance (or hub) base URL; env PROTOAGENT_URL; default http://127.0.0.1:7870")
    p.add_argument("--token", help="bearer token (prefer --token-file / env PROTOAGENT_TOKEN)")
    p.add_argument("--token-file", help="file holding the bearer token; env PROTOAGENT_TOKEN_FILE")
    p.add_argument("--slug", help="reach a fleet member through the hub's /agents/<slug> proxy")
    p.add_argument("--root", action="append", type=_parse_root, default=[], metavar="NAME=PATH",
                   help="map a project name to a LOCAL absolute path (repeatable; required for remote instances)")
    p.add_argument("--context-prefix", default="chat-zed",
                   help="A2A contextId prefix; the default keeps sessions visible in the console's chat list")
    p.add_argument("--trace-frames", metavar="FILE", help="append every raw A2A frame (JSON lines) to FILE")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    p.add_argument("--version", action="version", version=f"protoagent-acp {__version__}")
    return p


async def _serve(args: argparse.Namespace) -> None:
    from acp import run_agent

    from .a2a import A2AClient
    from .agent import ProtoAgentACP
    from .roots import RootMap

    creds = credentials.resolve(args.url, args.token, args.token_file)
    logging.getLogger(__name__).info("protoAgent at %s (credential: %s)", creds.url, creds.source)
    trace = open(args.trace_frames, "a", encoding="utf-8") if args.trace_frames else None  # noqa: SIM115
    client = A2AClient(creds.url, creds.token, slug=args.slug, trace=trace)
    roots = RootMap(dict(args.root))
    agent = ProtoAgentACP(
        client,
        roots,
        context_prefix=args.context_prefix,
        reload_credentials=lambda: credentials.resolve(args.url, args.token, args.token_file),
    )
    try:
        await run_agent(agent)
    finally:
        await client.aclose()
        if trace is not None:
            trace.close()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="protoagent-acp %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO and httpcore dumps wire detail at DEBUG — keep both
    # quiet so a verbose log never carries request internals.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.command == "login":
        sys.exit(credentials.login(args.url))
    asyncio.run(_serve(args))
