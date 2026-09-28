"""The in-flight-limiter docs must name what the code actually shipped (ADR 0115, #3760).

The C7 docs card (the guide, the two reference edits, the nav) has one standing hazard:
it hand-writes config keys, defaults, metric names, an endpoint path and a set of priority
classes that are *defined elsewhere in this repo* — C3's config, C6's metrics + route, the
C2 limiter module. When one of those moves and the prose doesn't, the doc lies under a green
gate. This is the `test_keybinding_docs.py` / `test_plugin_view_bridge_docs.py` treatment for
the limiter pages: every claim below is re-derived from the shipped source, so the test
reddens the moment the docs drift from the code.

Nothing here imports the server or the config stack — `graph.llm_limiter` is host-free by
design (ADR 0115 C2), and the rest is read as source text so the test stays cheap and
self-contained.
"""

from __future__ import annotations

import json
from pathlib import Path

from graph import llm_limiter

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "model-concurrency.md"
CONFIG_REF = REPO / "docs" / "reference" / "configuration.md"
API_REF = REPO / "docs" / "reference" / "operator-api.md"

METRICS_SRC = REPO / "observability" / "metrics.py"
SCHEMA_SRC = REPO / "graph" / "settings_schema.py"
ROUTES_SRC = REPO / "operator_api" / "telemetry_routes.py"

ENDPOINT = "/api/telemetry/llm-lanes"
CONFIG_KEYS = ("model.max_inflight", "model.inflight_queue_timeout", "model.inflight_interactive_reserve")
METRIC_SUFFIXES = ("llm_inflight", "llm_queue_depth", "llm_queue_wait_seconds", "llm_queue_timeouts_total")


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_guide_exists_and_links_resolve() -> None:
    """The guide is written and its two required cross-links point at files that exist —
    a dead relative link fails the docs build (`ignoreDeadLinks: 'localhostLinks'`)."""
    guide = _text(GUIDE)
    for target in ("../adr/0115-gateway-inflight-limiter.md", "../explanation/litellm-gateway.md"):
        assert target in guide, f"guide must link {target}"
        assert (GUIDE.parent / target).resolve().is_file(), f"dead link: {target}"


def test_guide_names_the_shipped_priority_classes_and_error() -> None:
    """`interactive` / `default` / `bulk` and `GatewayQueueTimeout` are the limiter's own
    names — re-derived from the module so a rename can't leave the prose behind."""
    guide = _text(GUIDE)
    assert llm_limiter.PRIORITIES == ("interactive", "default", "bulk")  # guards the tuple below
    for cls in llm_limiter.PRIORITIES:
        assert cls in guide, f"guide must name the {cls!r} priority class"
    assert llm_limiter.GatewayQueueTimeout.__name__ in guide


def test_documented_defaults_match_the_limiter_module() -> None:
    """The defaults the config reference advertises (0 / 300 / 1) are the limiter's own
    process-wide defaults. Read them back from a freshly reset module, not a literal."""
    llm_limiter._reset_for_tests()
    assert (llm_limiter._LIMIT, llm_limiter._QUEUE_TIMEOUT, llm_limiter._RESERVE) == (0, 300.0, 1)
    cfg = _text(CONFIG_REF)
    # The three rows sit in the `model` table with their default in the second column.
    assert "| `max_inflight` | `0` |" in cfg
    assert "| `inflight_queue_timeout` | `300` |" in cfg
    assert "| `inflight_interactive_reserve` | `1` |" in cfg


def test_config_reference_keys_are_the_ones_the_schema_ships() -> None:
    """Every `model.*` key the reference documents is a key C3's FIELDS actually parse —
    so a doc can't advertise a setting the schema doesn't render."""
    schema = _text(SCHEMA_SRC)
    cfg = _text(CONFIG_REF)
    for key in CONFIG_KEYS:
        assert key in schema, f"{key} is not in settings_schema.py — stale doc key"
        short = key.split(".", 1)[1]
        assert f"`{short}`" in cfg, f"configuration.md must document {short}"


def test_metric_names_match_what_metrics_py_registers() -> None:
    """The four D8 series are named in the guide exactly as `observability/metrics.py`
    registers them (prefix aside), so the scrape examples stay real."""
    metrics = _text(METRICS_SRC)
    guide = _text(GUIDE)
    for suffix in METRIC_SUFFIXES:
        assert f"_{suffix}" in metrics, f"{suffix} is not registered in metrics.py"
        assert suffix in guide, f"guide must name the {suffix} metric"


def test_endpoint_documented_and_wired() -> None:
    """`GET /api/telemetry/llm-lanes` is the route the telemetry registrar serves, and it's
    documented in both the operator-API reference and the guide, with the off-state shape."""
    assert f'"{ENDPOINT}"' in _text(ROUTES_SRC), "route not registered in telemetry_routes.py"
    api = _text(API_REF)
    guide = _text(GUIDE)
    assert ENDPOINT in api
    assert ENDPOINT in guide
    # The documented degraded shape must match the route's `{"enabled": false}` return.
    assert '{"enabled": false}' in api


def test_guide_is_in_the_generated_docs_nav() -> None:
    """`gen_docs_nav.py` must have picked the guide up from the sidebar (its `--check` is a
    separate CI gate; this pins the committed artifact carries the page)."""
    nav = json.loads(_text(REPO / "plugins" / "docs" / "nav.json"))
    paths = {it["path"] for groups in nav.values() for grp in groups for it in grp["items"]}
    assert "guides/model-concurrency.md" in paths
