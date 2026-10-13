"""``protoagent knowledge ingest <source>`` — ingest a URL / file into this instance's
knowledge base from the terminal (ADR 0075 D2).

The CLI projection of the ``knowledge_ingest`` agent tool + the ``/api/knowledge/ingest``
route: all three now run the one shared ``ops.knowledge.ingest`` op. This verb lives in
``server/`` (not a ``graph/**/cli.py`` forward) because it needs to boot the instance's
stores standalone — it reuses the operator-MCP sidecar's ``_boot_stores_only`` (which also
applies any plugin knowledge backend, so the CLI writes to the SAME store the running
instance uses; WAL makes it safe against a live server). Ingest runs inline here — a big
URL/media source blocks the command until it's indexed (there's no background loop in a
one-shot CLI process).
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def run_knowledge_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="protoagent knowledge", description="Ingest sources into this instance's knowledge base (ADR 0075)."
    )
    sub = parser.add_subparsers(dest="action", required=True)
    ip = sub.add_parser("ingest", help="fetch/extract a URL or local file and index it")
    ip.add_argument("source", help="an http(s) URL or a local file path")
    ip.add_argument("--domain", default="general", help="knowledge bucket to file it under (default: general)")
    ip.add_argument("--title", default=None, help="optional heading (else the source's own title)")
    args = parser.parse_args(argv)

    if args.action == "ingest":
        return _ingest(args.source, args.domain, args.title)
    return 2  # unreachable: argparse rejects an unknown action first


def _ingest(source: str, domain: str, title: str | None) -> int:
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from ops import OpContext
    from ops.knowledge import IngestError, IngestSource, ingest
    from server.operator_mcp import _boot_stores_only

    config = LangGraphConfig.from_yaml(config_yaml_path())
    _boot_stores_only(config)  # build just the stores (+ plugin knowledge backend), no graph/loops

    src = source.strip()
    is_url = src.lower().startswith(("http://", "https://"))
    ingest_source = IngestSource.from_url(src) if is_url else IngestSource.from_path(src)
    try:
        result = asyncio.run(ingest(ingest_source, domain=domain, title=title, ctx=OpContext.from_state()))
    except IngestError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 1
    label = result.title or result.source
    print(f"ingested {label!r} ({result.source_type}, {result.chars} chars) → {result.chunks} chunk(s) in {domain!r}")
    return 0
