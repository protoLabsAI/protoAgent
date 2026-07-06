"""Code-review findings convention (ADR 0077) — the schema, parser, and renderer
shared by the `code-review` workflow's prompts and its consumers (the craft
skill, the board review gate, the console)."""

from graph.review.findings import (
    FINDINGS_CONTRACT,
    Finding,
    parse_findings,
    render_findings_markdown,
)

__all__ = ["FINDINGS_CONTRACT", "Finding", "parse_findings", "render_findings_markdown"]
