"""The "Call a model directly" guide must name what the passthrough route actually shipped (#4608).

The guide (docs/guides/model-passthrough.md) hand-writes the URL shape, the auth/loopback rule,
the ``model_passthrough.allow_remote`` setting and its default, the usage attribution, and the
eval example — all of which are *defined elsewhere in this repo*: the FastAPI route in
``operator_api/model_passthrough_routes.py``, the ``model_passthrough.allow_remote`` FIELD in
``graph/settings_schema.py``, and its dataclass default in ``graph/config.py``. When one of those
moves and the prose doesn't, the doc lies under a green gate. This is the
``test_stay_awake_docs.py`` / ``test_model_concurrency_docs.py`` treatment for the passthrough
page: every claim below is re-derived from the shipped source (read as text so the test stays
cheap and self-contained, no server import), so the test reddens the moment the docs drift from
the code.

It also pins the two wiring facts a new journey page needs: a disposition in the docs-media
registry (``docs/dev/media/registry.json`` — omitting it is what reddened CI on the first attempt)
and a primary home in the catalog that puts it in the generated sidebar config ``config.mts``
consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "model-passthrough.md"
ROUTE_SRC = REPO / "src" / "operator_api" / "model_passthrough_routes.py"
SCHEMA_SRC = REPO / "src" / "graph" / "settings_schema.py"
CONFIG_SRC = REPO / "src" / "graph" / "config.py"
CATALOG = REPO / "plugins" / "docs" / "catalog.json"
NAVIGATION = REPO / "docs" / ".vitepress" / "navigation.json"
MEDIA_REGISTRY = REPO / "docs" / "dev" / "media" / "registry.json"
FRAGMENT = REPO / "changelog.d" / "4608.docs.md"
CHANGELOG_MD = REPO / "CHANGELOG.md"

PAGE = "guides/model-passthrough.md"
ROUTE_PATH = "/api/models/{connection}/v1/chat/completions"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _flat(s: str) -> str:
    """Collapse runs of whitespace to single spaces — Markdown reflows soft line breaks, so a
    phrase the guide wraps mid-line (e.g. a quoted Settings label) still reads as one string."""
    return " ".join(s.split())


def test_guide_exists_and_leads_with_the_not_the_agent_v1_distinction() -> None:
    """The first paragraph must draw the distinction the card asks for up front: a pure model
    call, NOT the agent's ``/v1`` graph turn (no graph, no tools, no SOUL/memory)."""
    guide = _text(GUIDE)
    assert guide.startswith("# Call a model directly"), "the documented page title"
    intro = guide.split("\n## ", 1)[0]  # everything before the first section heading
    assert "not the agent's `/v1" in intro, "the agent-/v1 distinction must be up front"
    assert "no graph" in intro and "no tools" in intro, "no graph / no tools run"
    assert "SOUL" in intro and "memory" in intro, "adds no SOUL or memory"


def test_guide_documents_the_shipped_route_path() -> None:
    """The URL shape the guide advertises is the route's own decorator path."""
    route = _text(ROUTE_SRC)
    assert f'"{ROUTE_PATH}"' in route, "route decorator must own this path"
    guide = _text(GUIDE)
    assert f"POST {ROUTE_PATH}" in guide, "guide must advertise the shipped URL shape"


def test_guide_documents_auth_loopback_and_the_status_codes() -> None:
    """Bearer auth, the loopback-only default, and the 403/400/404/502 outcomes the route
    raises are re-derived from the route source."""
    route = _text(ROUTE_SRC)
    guide = _text(GUIDE)
    # Loopback-only default + its 403, straight off the route gate.
    assert "status_code=403" in route and "loopback-only" in route
    assert "loopback-only" in guide and "403" in guide
    # The other error codes the route raises, each documented.
    assert "status_code=404" in route and "404" in guide, "unknown connection → 404"
    assert "status_code=400" in route and "400" in guide, "missing model / streaming → 400"
    assert "status_code=502" in route and "502" in guide, "model failure → 502"
    # Bearer, not the provider key.
    assert "Authorization: Bearer" in guide


def test_guide_documents_non_streaming_only() -> None:
    """The route rejects ``stream: true``; the guide must say streaming is unsupported."""
    route = _text(ROUTE_SRC)
    assert "if body.stream:" in route and "streaming is not supported" in route
    guide = _text(GUIDE)
    assert "non-streaming" in guide
    assert "Streaming is not supported" in guide


def test_guide_documents_allow_remote_setting_and_default() -> None:
    """The ``model_passthrough.allow_remote`` setting, its Settings label, and its false default
    are re-derived from the schema FIELD and the config dataclass."""
    schema = _text(SCHEMA_SRC)
    config = _text(CONFIG_SRC)
    assert '"model_passthrough.allow_remote",' in schema, "schema must ship the setting key"
    assert '"Allow remote callers",' in schema, "the shipped label the guide quotes"
    assert "model_passthrough_allow_remote: bool = False" in config, "default must be false"

    guide = _text(GUIDE)
    flat = _flat(guide)
    assert "Allow remote callers" in flat, "guide quotes the Settings toggle label"
    # The YAML the guide shows resolves to the schema's dotted key.
    assert "model_passthrough:" in guide and "allow_remote: true" in guide
    assert "The default is `false`." in guide
    # Enabling it lifts only the loopback restriction; bearer still applies.
    assert "bearer authentication still" in flat.lower() or "bearer still applies" in flat.lower()


def test_guide_documents_usage_attribution() -> None:
    """Usage lands in ``/usage`` keyed by the route's ``_ORIGIN`` prefix for both the session id
    and the task id — re-derived from the route source."""
    route = _text(ROUTE_SRC)
    assert '_ORIGIN = "model_passthrough"' in route, "the row-key prefix the guide names"
    assert 'session_id=f"{_ORIGIN}:{conn}"' in route, "session id is model_passthrough:<connection>"
    guide = _text(GUIDE)
    assert "/usage" in guide and "/api/usage" in guide
    assert "model_passthrough:<connection>" in guide, "session-id attribution"
    assert "`model_passthrough:` task ID" in guide, "task-id attribution"


def test_guide_documents_tools_to_tool_calls_roundtrip() -> None:
    """``tools`` in → ``tool_calls`` out, with ``finish_reason`` of ``tool_calls`` or ``stop`` —
    the two outcomes the route's ``build_chat_completion`` produces."""
    route = _text(ROUTE_SRC)
    assert '"tool_calls" if message.get("tool_calls") else "stop"' in route
    guide = _text(GUIDE)
    assert "tool_calls" in guide and "finish_reason" in guide
    assert "`stop`" in guide


def test_guide_documents_native_subscription_connections() -> None:
    """Both native-subscription connection types the route special-cases are named in the
    guide (they are the whole reason the endpoint exists)."""
    route = _text(ROUTE_SRC)
    assert '"anthropic-oauth"' in route and '"openai-codex"' in route
    guide = _text(GUIDE)
    assert "anthropic-oauth" in guide and "openai-codex" in guide


def test_guide_carries_the_eval_example() -> None:
    """The motivating eval invocation — base URL on the connection's ``/v1`` prefix, the two env
    vars, and the careercoach script path — must be present verbatim enough to copy."""
    guide = _text(GUIDE)
    assert "EVAL_BASE_URL=http://127.0.0.1:<port>/api/models/anthropic-oauth/v1" in guide
    assert "EVAL_MODEL=" in guide
    assert "evals/tool_selection/run_eval.py" in guide


def test_page_has_a_media_disposition() -> None:
    """A current journey page must carry a docs-media disposition; omitting it fails
    ``scripts/docs_media.py --check`` (``load_registry``). Pin it so the page can never be added
    to the catalog without its registry row again."""
    registry = json.loads(_text(MEDIA_REGISTRY))
    assert PAGE in registry["pages"], f"{PAGE} needs a docs-media disposition"
    audit = registry["pages"][PAGE]
    assert audit["decision"] in {"media", "text", "reuse"} and audit["reason"]


def test_page_is_wired_into_the_catalog_and_sidebar() -> None:
    """The page needs a primary home in the catalog (so gen_docs_nav classifies it) and must
    land in the generated navigation ``config.mts`` builds the sidebar from."""
    catalog = json.loads(_text(CATALOG))
    assert PAGE in catalog["pages"], "page must be classified in the catalog"
    homes = [
        p
        for j in catalog["journeys"]
        for g in j["groups"]
        for p in g["pages"]
    ] + [j["landing"] for j in catalog["journeys"]]
    assert PAGE in homes, "page must have a primary navigation home"
    # The generated sidebar config (consumed by config.mts) must link the page.
    assert "/guides/model-passthrough" in _text(NAVIGATION)


def test_changelog_records_the_guide() -> None:
    """A collatable ``docs`` fragment references the PR pre-release; after collation the released
    lead-in lands in CHANGELOG.md (or a monthly archive)."""
    if FRAGMENT.exists():
        text = _text(FRAGMENT)
        assert text.lstrip().startswith("- **"), "fragment needs the load-bearing top-level bullet"
        assert "#4608" in text, "fragment must reference the issue"
    else:
        released = _text(CHANGELOG_MD) if CHANGELOG_MD.exists() else ""
        archives = "".join(_text(p) for p in REPO.glob("CHANGELOG-*.md"))
        assert "passthrough" in (released + archives).lower()
