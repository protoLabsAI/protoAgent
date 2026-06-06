#!/usr/bin/env python3
"""Scan audit.jsonl for unredacted credential patterns.

Usage:
    python tests/utils/check_audit_clean.py [audit.jsonl]

Exits with code 0 if no credentials found, 1 if any are detected.
Use in CI/CD to gate deployments on clean audit logs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple


class _Violation(NamedTuple):
    line_number: int
    pattern_name: str
    excerpt: str


# Patterns that should NOT appear in a clean audit log.
_LEAK_PATTERNS: dict[str, re.Pattern] = {
    "bearer_token": re.compile(
        r"Authorization\s*:\s*Bearer\s+(?!\[REDACTED\])\S+",
        re.IGNORECASE,
    ),
    "openai_key": re.compile(
        r"\bsk-[A-Za-z0-9_\-]{20,}\b",
    ),
    "generic_api_key": re.compile(
        r"(?i)(?:api[_\-]?key|apikey)(?:[\"'\s:=]+)(?!\[REDACTED\])([A-Za-z0-9_\-]{16,})",
    ),
    "env_var_assignment": re.compile(
        r"(?i)\b(?:OPENAI_API_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_PUBLIC_KEY|"
        r"A2A_AUTH_TOKEN|SECRET_KEY|AUTH_TOKEN|ACCESS_TOKEN|PRIVATE_KEY)"
        r"\s*[=:]\s*(?!\[REDACTED\])\S+",
    ),
}


def scan_file(path: Path) -> list[_Violation]:
    """Scan a JSONL file and return a list of credential violations found."""
    violations: list[_Violation] = []

    if not path.exists():
        print(f"[check_audit_clean] File not found: {path}", file=sys.stderr)
        return violations

    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        # Scan the raw serialized line (catches nested values too)
        for pattern_name, pattern in _LEAK_PATTERNS.items():
            m = pattern.search(raw_line)
            if m:
                # Truncate excerpt for readability
                start = max(0, m.start() - 20)
                end = min(len(raw_line), m.end() + 20)
                excerpt = raw_line[start:end]
                violations.append(_Violation(lineno, pattern_name, excerpt))

    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: check_audit_clean.py <audit.jsonl>", file=sys.stderr)
        return 1

    path = Path(argv[0])
    violations = scan_file(path)

    if not violations:
        print(f"[check_audit_clean] {path}: CLEAN — no credentials detected.")
        return 0

    print(f"[check_audit_clean] {path}: VIOLATIONS FOUND ({len(violations)})")
    for v in violations:
        print(f"  line {v.line_number}: [{v.pattern_name}] ...{v.excerpt}...")
    return 1


if __name__ == "__main__":
    sys.exit(main())
