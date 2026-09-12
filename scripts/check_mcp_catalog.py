#!/usr/bin/env python3
"""Check every curated MCP quick-add entry against upstream (drift guard, #2910).

``config/mcp-catalog.json`` is hand-edited, and its failure mode is invisible from the
repo: an entry only runs when an operator clicks it in Settings ▸ MCP ▸ Browse, so a
package that upstream renamed, deprecated or pulled fails in front of a user and
nowhere else. That happened: the catalog shipped
``@modelcontextprotocol/server-sequentialthinking`` for weeks after the package moved
to ``server-sequential-thinking``. tests/test_mcp_catalog.py covers everything that can
go stale offline; this covers what needs the network:

  npx <pkg>      the npm package exists and its latest version isn't deprecated
  uvx <pkg>      the PyPI project exists and its latest release isn't yanked
  http/sse url   the endpoint answers (a 401/403/405 without credentials is alive;
                 404/410 is gone)
  docs           the docs link resolves

    python scripts/check_mcp_catalog.py              # human-readable
    python scripts/check_mcp_catalog.py --markdown   # the tracking-issue body

A result is DRIFT only when upstream answered and said no (404/410, deprecated, yanked).
Anything inconclusive — network error, timeout, 5xx, rate limit — is a warning: a
flaky registry must not file an issue or redden a PR, and the weekly run retries.

Exit codes: 0 = every entry resolves (warnings allowed), 1 = drift found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

CATALOG = Path(__file__).parent.parent / "config" / "mcp-catalog.json"
_TIMEOUT = 20
_USER_AGENT = "protoAgent-mcp-catalog-check (+https://github.com/protoLabsAI/protoAgent)"

# (status, parsed JSON body or None). Raises _Unreachable for network-level failures.
Fetch = Callable[[str], "tuple[int, object]"]


class _Unreachable(RuntimeError):
    """No HTTP answer at all (DNS, refused, timeout) — inconclusive, never drift."""


class Target(NamedTuple):
    """One upstream thing a catalog entry depends on."""

    kind: str  # "npm" | "pypi" | "endpoint" | "docs" | "unsupported"
    ref: str  # package name, URL, or (for unsupported) why the entry can't be checked


class Result:
    """What checking one entry found. (Plain class, not a dataclass: the tests load this
    script by path, and dataclasses can't resolve annotations for an unregistered module.)"""

    def __init__(self, server: str) -> None:
        self.server = server
        self.drift: list[str] = []
        self.warnings: list[str] = []


# ── what an entry depends on (pure — unit-tested offline) ─────────────────────────────


def npm_package(spec: str) -> str:
    """``@scope/name@1.2`` → ``@scope/name``; ``name@latest`` → ``name``."""
    at = spec.rfind("@")
    return spec[:at] if at > 0 else spec


def pypi_package(spec: str) -> str:
    """``mcp-server-git==1.0`` / ``pkg@1.0`` / ``pkg[extra]>=2`` → the project name."""
    return re.split(r"[\[<>=!~@;\s]", spec, maxsplit=1)[0]


def _first_positional(args: list[str]) -> str | None:
    return next((a for a in args if not a.startswith("-")), None)


def targets(server: dict) -> list[Target]:
    """Everything this entry needs from upstream. An entry whose launcher this script
    doesn't understand yields an ``unsupported`` target, which the offline suite
    refuses — so a new kind of entry can't slip past the guard unchecked."""
    template = server.get("template") or {}
    out: list[Target] = []
    transport = template.get("transport", "stdio")
    if transport == "stdio":
        command = template.get("command") or ""
        args = [str(a) for a in template.get("args") or []]
        if command == "npx":
            spec = _first_positional(args)
            out.append(Target("npm", npm_package(spec)) if spec else Target("unsupported", "npx with no package"))
        elif command == "uvx":
            if "--from" in args and args.index("--from") + 1 < len(args):
                spec = args[args.index("--from") + 1]
            else:
                spec = _first_positional(args)
            out.append(Target("pypi", pypi_package(spec)) if spec else Target("unsupported", "uvx with no package"))
        else:
            out.append(Target("unsupported", f"stdio command {command!r} (only npx/uvx are checked)"))
    elif template.get("url"):
        out.append(Target("endpoint", template["url"]))
    else:
        out.append(Target("unsupported", f"{transport} entry without a url"))
    if server.get("docs"):
        out.append(Target("docs", server["docs"]))
    return out


# ── talking to upstream ───────────────────────────────────────────────────────────────


def _fetch(url: str) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json, */*"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 — https URLs from the catalog
            body = resp.read(4_000_000)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Unreachable(str(getattr(exc, "reason", exc))) from exc
    try:
        return status, json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return status, None


def _check_npm(name: str, fetch: Fetch, res: Result) -> None:
    status, doc = fetch(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}")
    if status == 404:
        res.drift.append(f"npm package `{name}` does not exist")
        return
    if status != 200 or not isinstance(doc, dict):
        res.warnings.append(f"npm registry answered {status} for `{name}`")
        return
    latest = (doc.get("dist-tags") or {}).get("latest")
    meta = (doc.get("versions") or {}).get(latest) or {}
    if meta.get("deprecated"):
        res.drift.append(f"npm package `{name}@{latest}` is deprecated: {meta['deprecated']}")


def _check_pypi(name: str, fetch: Fetch, res: Result) -> None:
    status, doc = fetch(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if status == 404:
        res.drift.append(f"PyPI project `{name}` does not exist")
        return
    if status != 200 or not isinstance(doc, dict):
        res.warnings.append(f"PyPI answered {status} for `{name}`")
        return
    latest = (doc.get("info") or {}).get("version")
    files = (doc.get("releases") or {}).get(latest) or []
    if files and all(f.get("yanked") for f in files):
        res.drift.append(f"PyPI `{name}` {latest} is yanked")


def _check_url(kind: str, url: str, fetch: Fetch, res: Result) -> None:
    status, _ = fetch(url)
    if status in (404, 410):
        res.drift.append(f"{kind} `{url}` answered {status}")
    elif status in (408, 429) or status >= 500:  # timeout / rate limit / server trouble
        res.warnings.append(f"{kind} `{url}` answered {status}")
    elif kind == "docs" and status >= 400:
        # A docs page is public: 401/403 there means it moved behind a login.
        res.drift.append(f"docs `{url}` answered {status}")
    # endpoint 401/403/405/406 without credentials = alive; 2xx/3xx = fine


def check_server(server: dict, fetch: Fetch | None = None) -> Result:
    fetch = fetch or _fetch  # resolved per call, so a test can swap the module's fetcher
    res = Result(server.get("id") or "?")
    for t in targets(server):
        try:
            if t.kind == "npm":
                _check_npm(t.ref, fetch, res)
            elif t.kind == "pypi":
                _check_pypi(t.ref, fetch, res)
            elif t.kind in ("endpoint", "docs"):
                _check_url(t.kind, t.ref, fetch, res)
            else:
                res.warnings.append(f"not checked: {t.ref}")
        except _Unreachable as exc:
            res.warnings.append(f"{t.kind} `{t.ref}` unreachable: {exc}")
    return res


def load(path: Path = CATALOG) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["servers"]


def render(results: list[Result], markdown: bool) -> str:
    lines: list[str] = []
    drifted = [r for r in results if r.drift]
    if markdown:
        lines.append(
            f"{len(drifted)} of {len(results)} MCP quick-add entries in `config/mcp-catalog.json` no longer "
            "resolve upstream. An operator who clicks one in Settings ▸ MCP ▸ Browse gets a failure."
        )
        lines.append("")
    for r in results:
        if not (r.drift or r.warnings):
            if not markdown:
                lines.append(f"ok    {r.server}")
            continue
        for d in r.drift:
            lines.append(f"- **{r.server}**: {d}" if markdown else f"DRIFT {r.server}: {d}")
        for w in r.warnings:
            lines.append(f"- {r.server}: _warning_ — {w}" if markdown else f"warn  {r.server}: {w}")
    if markdown:
        lines += ["", "Run locally: `python scripts/check_mcp_catalog.py`"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markdown", action="store_true", help="emit the tracking-issue body")
    args = parser.parse_args(argv)
    results = [check_server(s) for s in load()]
    print(render(results, args.markdown))
    return 1 if any(r.drift for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
