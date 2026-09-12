"""Offline tests for scripts/check_mcp_catalog.py — the upstream drift guard (#2910).

The script itself needs the network (a scheduled workflow runs it); everything here
injects the fetcher, so the verdict logic — what is drift, what is only a warning — is
pinned without touching a registry.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_mcp_catalog", Path(__file__).parent.parent / "scripts" / "check_mcp_catalog.py"
)
cmc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cmc)


def _fetcher(routes: dict[str, tuple[int, object] | Exception]):
    """A fetch that answers from ``routes`` (prefix match) and fails loudly on anything else."""

    def fetch(url: str):
        for prefix, answer in routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


def _npx(pkg: str, docs: str = "https://docs.example/x") -> dict:
    return {"id": "x", "docs": docs, "template": {"transport": "stdio", "command": "npx", "args": ["-y", pkg]}}


def _uvx(*args: str, docs: str = "https://docs.example/x") -> dict:
    return {"id": "x", "docs": docs, "template": {"transport": "stdio", "command": "uvx", "args": list(args)}}


_DOCS_OK = {"https://docs.example/": (200, None)}


# ── the real catalog is fully covered ────────────────────────────────────────────────


@pytest.mark.parametrize("server", cmc.load(), ids=lambda s: s["id"])
def test_every_catalog_entry_is_checkable(server: dict) -> None:
    """A new kind of entry (a docker image, a bare binary) would otherwise pass the
    guard unchecked forever. Teach check_mcp_catalog.py the launcher, then add it."""
    kinds = [t.kind for t in cmc.targets(server)]
    assert "unsupported" not in kinds, f"{server['id']}: {cmc.targets(server)}"
    assert kinds.count("docs") == 1
    assert len([k for k in kinds if k in ("npm", "pypi", "endpoint")]) == 1


# ── what a launcher depends on ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("@modelcontextprotocol/server-memory", "@modelcontextprotocol/server-memory"),
        ("@brave/brave-search-mcp-server@2.0.1", "@brave/brave-search-mcp-server"),
        ("some-server@latest", "some-server"),
        ("plain", "plain"),
    ],
)
def test_npm_package_strips_the_version(spec: str, name: str) -> None:
    assert cmc.npm_package(spec) == name


@pytest.mark.parametrize(
    ("spec", "name"),
    [("mcp-server-git", "mcp-server-git"), ("mcp-server-git==1.2", "mcp-server-git"),
     ("pkg[cli]>=2", "pkg"), ("pkg@1.0", "pkg")],
)
def test_pypi_package_strips_the_specifier(spec: str, name: str) -> None:
    assert cmc.pypi_package(spec) == name


def test_uvx_from_names_the_package_not_the_command() -> None:
    [pkg, _docs] = cmc.targets(_uvx("--from", "awesome-mcp==0.3", "awesome-server", "--flag"))
    assert pkg == cmc.Target("pypi", "awesome-mcp")


def test_an_unknown_launcher_is_reported_not_skipped() -> None:
    server = {"id": "x", "docs": "https://d", "template": {"transport": "stdio", "command": "docker", "args": ["run"]}}
    assert cmc.targets(server)[0].kind == "unsupported"
    res = cmc.check_server(server, _fetcher({"https://d": (200, None)}))
    assert not res.drift and any("not checked" in w for w in res.warnings)


# ── verdicts ─────────────────────────────────────────────────────────────────────────


def test_the_renamed_package_that_actually_shipped_is_caught() -> None:
    """The incident this guard exists for: the old sequential-thinking name stayed in
    the catalog for weeks after upstream moved it. The registry 404s the old name."""
    old = "@modelcontextprotocol/server-sequentialthinking"
    res = cmc.check_server(_npx(old), _fetcher({"https://registry.npmjs.org/": (404, None), **_DOCS_OK}))
    assert res.drift == [f"npm package `{old}` does not exist"]


def test_a_deprecated_npm_package_is_drift() -> None:
    doc = {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"deprecated": "moved to @x/y"}}}
    res = cmc.check_server(_npx("old-pkg"), _fetcher({"https://registry.npmjs.org/": (200, doc), **_DOCS_OK}))
    assert res.drift == ["npm package `old-pkg@1.0.0` is deprecated: moved to @x/y"]


def test_a_live_npm_package_passes() -> None:
    doc = {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {}}}
    res = cmc.check_server(_npx("ok"), _fetcher({"https://registry.npmjs.org/": (200, doc), **_DOCS_OK}))
    assert (res.drift, res.warnings) == ([], [])


def test_a_scoped_npm_name_is_encoded_for_the_registry() -> None:
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        return (200, {"dist-tags": {"latest": "1"}, "versions": {"1": {}}}) if "npmjs" in url else (200, None)

    cmc.check_server(_npx("@scope/pkg"), fetch)
    assert seen[0] == "https://registry.npmjs.org/@scope%2Fpkg"


def test_missing_and_yanked_pypi_projects_are_drift() -> None:
    gone = cmc.check_server(_uvx("nope"), _fetcher({"https://pypi.org/": (404, None), **_DOCS_OK}))
    assert gone.drift == ["PyPI project `nope` does not exist"]
    yanked_doc = {"info": {"version": "2.0"}, "releases": {"2.0": [{"yanked": True}, {"yanked": True}]}}
    yanked = cmc.check_server(_uvx("pkg"), _fetcher({"https://pypi.org/": (200, yanked_doc), **_DOCS_OK}))
    assert yanked.drift == ["PyPI `pkg` 2.0 is yanked"]


def test_a_remote_endpoint_that_wants_credentials_is_alive() -> None:
    """The GitHub MCP endpoint answers an anonymous probe with 401 — that is up."""
    server = {"id": "gh", "docs": "https://docs.example/x", "template": {"transport": "http", "url": "https://mcp.example/"}}
    for status in (200, 401, 403, 405, 406):
        res = cmc.check_server(server, _fetcher({"https://mcp.example/": (status, None), **_DOCS_OK}))
        assert (res.drift, res.warnings) == ([], []), status
    gone = cmc.check_server(server, _fetcher({"https://mcp.example/": (404, None), **_DOCS_OK}))
    assert gone.drift == ["endpoint `https://mcp.example/` answered 404"]


def test_a_docs_page_behind_a_login_or_gone_is_drift() -> None:
    doc = {"dist-tags": {"latest": "1"}, "versions": {"1": {}}}
    for status in (403, 404, 410):
        res = cmc.check_server(_npx("ok"), _fetcher({"https://registry.npmjs.org/": (200, doc),
                                                     "https://docs.example/": (status, None)}))
        assert res.drift == [f"docs `https://docs.example/x` answered {status}"], status


@pytest.mark.parametrize(
    "answer",
    [(503, None), (429, None), (408, None), cmc._Unreachable("DNS failure")],
    ids=["5xx", "rate-limited", "timeout", "unreachable"],
)
def test_inconclusive_answers_warn_and_never_count_as_drift(answer) -> None:
    """A flaky registry must not file an issue or redden a PR."""
    res = cmc.check_server(_npx("pkg"), _fetcher({"https://registry.npmjs.org/": answer,
                                                  "https://docs.example/": answer}))
    assert res.drift == [] and len(res.warnings) == 2


def test_exit_code_is_drift_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cmc, "load", lambda: [_npx("pkg")])
    monkeypatch.setattr(cmc, "_fetch", _fetcher({"https://registry.npmjs.org/": (503, None), **_DOCS_OK}))
    assert cmc.main([]) == 0  # a warning alone
    monkeypatch.setattr(cmc, "_fetch", _fetcher({"https://registry.npmjs.org/": (404, None), **_DOCS_OK}))
    assert cmc.main(["--markdown"]) == 1
    body = capsys.readouterr().out
    assert "1 of 1 MCP quick-add entries" in body and "- **x**: npm package `pkg` does not exist" in body
