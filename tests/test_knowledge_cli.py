"""protoagent knowledge ingest (ADR 0075 D2) — the CLI projection of ops.knowledge.ingest.
The store boot + the op are faked (covered elsewhere); this tests the CLI wiring."""

from __future__ import annotations


def _patch(monkeypatch, ingest_fn):
    import graph.config as gc
    import graph.config_io as cio
    import ops.knowledge as opk
    import server.operator_mcp as omcp

    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(gc.LangGraphConfig, "from_yaml", classmethod(lambda cls, p: gc.LangGraphConfig()))
    monkeypatch.setattr(omcp, "_boot_stores_only", lambda config: None)
    monkeypatch.setattr(opk, "ingest", ingest_fn)


def test_knowledge_ingest_url(monkeypatch, capsys):
    from ops.knowledge import IngestResult, IngestSource
    from server.knowledge_cli import run_knowledge_cli

    seen: dict = {}

    async def _fake(src, *, domain, title, ctx):
        seen.update(src=src, domain=domain, title=title)
        return IngestResult(ids=[1, 2], chunks=2, chars=120, title="Doc", source_type="html", source="https://x")

    _patch(monkeypatch, _fake)
    assert run_knowledge_cli(["ingest", "https://x/post", "--domain", "research"]) == 0
    out = capsys.readouterr().out
    assert "2 chunk(s)" in out and "research" in out and "Doc" in out
    assert isinstance(seen["src"], IngestSource) and seen["src"].url == "https://x/post"
    assert seen["domain"] == "research"


def test_knowledge_ingest_file_path_uses_from_path(monkeypatch, capsys):
    from ops.knowledge import IngestResult
    from server.knowledge_cli import run_knowledge_cli

    seen: dict = {}

    async def _fake(src, *, domain, title, ctx):
        seen["src"] = src
        return IngestResult(ids=[1], chunks=1, chars=10, title=None, source_type="text", source="/tmp/a.txt")

    _patch(monkeypatch, _fake)
    assert run_knowledge_cli(["ingest", "/tmp/a.txt"]) == 0
    assert seen["src"].path == "/tmp/a.txt" and seen["src"].url is None  # non-URL → from_path


def test_knowledge_ingest_error_returns_1(monkeypatch, capsys):
    from ops.knowledge import IngestError
    from server.knowledge_cli import run_knowledge_cli

    async def _fake(src, *, domain, title, ctx):
        raise IngestError("No such file: x", kind="not_found")

    _patch(monkeypatch, _fake)
    assert run_knowledge_cli(["ingest", "/no/such"]) == 1
    assert "No such file" in capsys.readouterr().err
