"""A plugin id names a directory under the instance root (``sdk.plugin_store(plugin_id=<id>)``
→ ``instance_root/<id>``), the SAME namespace core's own stores use (part of #4808). So the
manifest parser must reject an id that collides with a core data directory (``memory``,
``knowledge``, …) just as it already rejects one that shadows a core ``/api/plugins/<verb>``
route (``install``), lest the plugin land on core's data.
"""

from __future__ import annotations

import logging as _logging
from pathlib import Path

from graph.plugins.manifest import _CORE_STORE_NAMES, _RESERVED_PLUGIN_IDS, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_PLUGINS_DIR = REPO_ROOT / "plugins"


def _make_manifest(root: Path, pid: str) -> Path:
    d = root / "plugin-dir"  # dir name intentionally != id, so only the manifest id is tested
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: false\n",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text("def register(registry):\n    pass\n", encoding="utf-8")
    return d


def test_core_store_name_id_is_rejected_with_the_directory_reason(tmp_path, caplog) -> None:
    d = _make_manifest(tmp_path, "memory")
    with caplog.at_level(_logging.WARNING, logger="protoagent.plugins"):
        assert load_manifest(d) is None  # `memory` IS core's memory store dir
    text = caplog.text.lower()
    assert "memory" in text
    assert "core data directory" in text  # the warning names WHY (not a route)


def test_core_store_name_match_is_case_insensitive(tmp_path, caplog) -> None:
    # `Knowledge` lower-cases onto `knowledge`, a core store dir — rejected like `memory`.
    d = _make_manifest(tmp_path, "Knowledge")
    with caplog.at_level(_logging.WARNING, logger="protoagent.plugins"):
        assert load_manifest(d) is None
    assert "core data directory" in caplog.text.lower()


def test_route_verb_id_is_still_rejected_with_the_route_reason(tmp_path, caplog) -> None:
    # The pre-existing reserved-route guard must keep working, with its own (route) reason.
    d = _make_manifest(tmp_path, "install")
    with caplog.at_level(_logging.WARNING, logger="protoagent.plugins"):
        assert load_manifest(d) is None
    text = caplog.text.lower()
    assert "route" in text
    assert "core data directory" not in text  # install is a route, not a data dir


def test_ordinary_id_is_accepted(tmp_path) -> None:
    # A slug that collides with neither reserved set loads normally — the guard is additive.
    d = _make_manifest(tmp_path, "my-cool-plugin")
    m = load_manifest(d)
    assert m is not None and m.id == "my-cool-plugin"


def test_friction_is_not_reserved() -> None:
    # The bundled `friction` plugin OWNS `instance_root/friction` by design (it writes there via
    # store("friction")), so reserving it would reject that plugin — it must stay allowed.
    assert "friction" not in _CORE_STORE_NAMES
    assert "friction" not in _RESERVED_PLUGIN_IDS


def test_the_named_core_stores_are_reserved() -> None:
    # The ids #4808 calls out explicitly must all be present.
    for name in ("knowledge", "memory", "media", "goals", "tasks", "audit", "scheduler", "watches"):
        assert name in _CORE_STORE_NAMES, name


def test_no_bundled_plugin_id_is_newly_rejected() -> None:
    # Reserving core store names must not reject any shipped plugin. Load every bundled manifest
    # and assert its id survives (not in either reserved set) — the regression guard for the
    # friction-style collision this card had to avoid.
    manifests = sorted(BUNDLED_PLUGINS_DIR.glob("*/protoagent.plugin.yaml"))
    assert manifests, "no bundled plugin manifests found — this guard would be vacuous"
    reserved = _CORE_STORE_NAMES | _RESERVED_PLUGIN_IDS
    offenders: list[str] = []
    for mf in manifests:
        got = load_manifest(mf.parent)
        assert got is not None, f"bundled plugin {mf.parent.name} failed to parse"
        if got.id.lower() in reserved:
            offenders.append(got.id)
    assert not offenders, f"bundled plugin id(s) newly rejected by a reserved set: {offenders}"
