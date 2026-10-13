"""Drift guard for the config triplet (refactor: config single source of truth).

Three structures must stay in lock-step:

* ``graph.settings_schema.FIELDS`` — the operator console's settings registry,
  mapping each dotted YAML ``key`` to the ``LangGraphConfig`` ``attr`` that holds
  its live value plus its UI ``type``.
* ``graph.config.LangGraphConfig`` — the live dataclass the runtime reads.
* ``graph.config_io.config_to_dict`` — the nested-dict serializer the UI round-
  trips through.

When these drift apart the failure is silent in production: the Settings UI
shows a field that never persists (missing dataclass attr), or a save round-trips
through a serializer that drops the section on the floor. These tests turn any
such drift into a CI failure.

Test #5 (2026-06-10) guards the third direction: ``LangGraphConfig.from_dict``
must CONSUME every FIELDS key — a missing parse line means the YAML holds the
value, config_to_dict shows it "saved", but the runtime silently reads the
default.

NOTE on the import path: ``config_to_dict`` lives in ``graph.config_io`` (not
``graph.config``) — it is imported from there below.

Each direction is ONE aggregate test that walks every FIELDS entry and names
every drifted key in its failure message (a per-field parametrized matrix used
to duplicate these ~500 times over; the known-gap xfail sets it carried were
empty, so it added cost without coverage).
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.config_io import config_to_dict
from graph.settings_schema import FIELDS

_SECRET_KEYS = {f.key for f in FIELDS if f.type == "secret"}


def _resolve(d: dict, dotted: str):
    """Walk ``dotted`` (e.g. ``"prompt_cache.warm.enabled"``) through nested ``d``.

    Returns ``(found, value)`` — ``found`` is False the moment any segment is
    absent or a non-dict is hit mid-walk.
    """
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


# --------------------------------------------------------------------------- #
# 1) Every FIELDS.attr exists on the dataclass.
# --------------------------------------------------------------------------- #


def test_attr_missing_set_is_exactly_as_expected():
    """Each settings Field must point at a real ``LangGraphConfig`` attribute.

    A missing attr means the Settings UI renders a control that reads
    ``None``/default and whose save never round-trips into the live config.
    Every offending key is named in the failure.
    """
    cfg = LangGraphConfig()
    missing = {f.key: f.attr for f in FIELDS if not hasattr(cfg, f.attr)}
    assert not missing, (
        f"FIELDS keys map to LangGraphConfig attributes that do not exist (key -> attr): "
        f"{missing} — settings drift. Add the dataclass field + from_dict parse line."
    )


# --------------------------------------------------------------------------- #
# 2) Every non-secret FIELDS key resolves in config_to_dict's output.
# --------------------------------------------------------------------------- #

_NON_SECRET_FIELDS = [f for f in FIELDS if f.key not in _SECRET_KEYS]


def test_config_to_dict_missing_set_is_exactly_as_expected():
    """Each non-secret settings key must round-trip through ``config_to_dict``.

    Secrets are excluded (config_to_dict deliberately redacts them, and may
    omit a never-set section). A key that the serializer drops means a Settings
    save can be silently lost on the next load. Every dropped key is named.
    """
    d = config_to_dict(LangGraphConfig())
    missing = sorted(f.key for f in _NON_SECRET_FIELDS if not _resolve(d, f.key)[0])
    assert not missing, (
        f"FIELDS keys do not resolve in config_to_dict() output: {missing} "
        f"(top-level keys: {sorted(d.keys())}) — serializer drift. Expand config_to_dict."
    )


# --------------------------------------------------------------------------- #
# 3) Field.type agrees with the dataclass default's Python type (best-effort).
# --------------------------------------------------------------------------- #

_DEFAULT_CFG = LangGraphConfig()


@pytest.mark.parametrize("field", FIELDS, ids=lambda f: f.key)
def test_fields_types_match_dataclass(field):
    """Best-effort: the declared UI ``type`` matches the dataclass default's type.

    * ``bool``        → default is a ``bool``.
    * ``number``      → default is ``int``/``float`` (and NOT ``bool``, which is
      an ``int`` subclass).
    * ``string_list`` → default is a ``list``.

    ``string``/``select``/``secret`` are intentionally not type-asserted: a blank
    default is often ``""`` and ``select`` values are plain strings, so there is
    nothing load-bearing to check beyond what the above three cover.
    """
    val = getattr(_DEFAULT_CFG, field.attr)
    if field.type == "bool":
        assert isinstance(val, bool), (
            f"{field.key!r} is type 'bool' but {field.attr} defaults to {type(val).__name__} ({val!r})."
        )
    elif field.type == "number":
        assert isinstance(val, (int, float)) and not isinstance(val, bool), (
            f"{field.key!r} is type 'number' but {field.attr} defaults to {type(val).__name__} ({val!r})."
        )
    elif field.type == "string_list":
        assert isinstance(val, list), (
            f"{field.key!r} is type 'string_list' but {field.attr} defaults to {type(val).__name__} ({val!r})."
        )


# --------------------------------------------------------------------------- #
# 4) Field.scope assignment (ADR 0047 §2.1 Decision 2) — the box-shared set.
# --------------------------------------------------------------------------- #

# The fields whose shared default lives at the HOST layer (gateway/model/routing/
# cache/telemetry infra + org branding + the box-shared skill commons location).
# Everything else is "agent". Locked here so a new Field can't silently land in the
# wrong cascade layer.
HOST_SCOPED_KEYS = {
    "model.api_base",
    "model.provider",
    "model.name",
    "routing.aux_model",
    "routing.fallback_models",
    "prompt_cache.enabled",
    "prompt_cache.ttl",
    "prompt_cache.warm.enabled",
    "prompt_cache.warm.interval_seconds",
    "telemetry.enabled",
    "telemetry.retention_days",
    "prompts.capture",
    "prompts.retention_days",
    "prompts.max_calls",
    "identity.org",
    # commons.path is box-level (ADR 0041 commons read by every agent on the box);
    # skills.scope stays "agent" (each agent picks its own sharing mode).
    "commons.path",
    # Box runtime (ADR 0047 D8) — env/CLI knobs promoted into the Host layer.
    "network.bind",
    # Egress allowlist (ADR 0008) — a box-wide outbound security policy, so it lives
    # at the Host layer alongside the inbound bind interface.
    "egress.allowed_hosts",
    "fleet.autostart",
    "fleet.port_base",
    "fleet.discovery.port_min",
    "fleet.discovery.port_max",
    "fleet.discovery.mdns",
    "fleet.warm.max",
    "fleet.warm.grace_seconds",
    # The memory ceiling bounds THIS PROCESS (#3365), so it belongs to the box, not
    # to an agent leaf — every co-located agent shares the process it would stop.
    "runtime.memory_ceiling_mb",
    "runtime.memory_ceiling_exit",
}


def test_field_scope_assignment_matches_adr_0047():
    """The host/agent split is exactly as decided (ADR 0047 §2.1). A new Field
    defaulting to 'agent' is fine; promoting one to 'host' (or vice-versa) must be
    a deliberate edit here, not silent drift."""
    live_host = {f.key for f in FIELDS if f.scope == "host"}
    assert live_host == HOST_SCOPED_KEYS, (
        f"host-scoped FIELDS changed: {live_host ^ HOST_SCOPED_KEYS} differ. "
        "Update HOST_SCOPED_KEYS + ADR 0047 §2.1 if intentional."
    )
    # Only "agent" / "host" are valid today (App layer = dataclass defaults, no field scope).
    bad = {f.scope for f in FIELDS} - {"agent", "host"}
    assert not bad, f"unexpected Field.scope value(s): {bad}"


# --------------------------------------------------------------------------- #
# 5) Every non-secret FIELDS key is CONSUMED by from_dict (third direction).
# --------------------------------------------------------------------------- #

# The third drift direction tests #1-#2 don't cover: a FIELDS key whose parse
# line is missing from ``LangGraphConfig.from_dict``. That failure is the
# nastiest of the three — the YAML holds the value, config_to_dict echoes it
# back (so the Settings UI shows it "saved"), but the live config silently
# reads the default. Audited 2026-06-10: EMPTY — every FIELDS key has its
# parse line today. If a field legitimately can't round-trip (computed /
# env-only), add its key here WITH a comment saying why; a bare addition to
# silence a red test is exactly the drift this set exists to make explicit.
FROM_DICT_UNCONSUMED_KEYS: set[str] = set()


def _sentinel_for(field) -> object:
    """A guaranteed NON-default value of the right shape for ``field``.

    Derived from the dataclass default's Python type (bool checked before int —
    it's an int subclass), falling back to the Field's declared UI type when the
    default is ``None``. Using the default as the base means the sentinel can
    never accidentally equal it.
    """
    default = getattr(_DEFAULT_CFG, field.attr)
    if isinstance(default, bool):
        return not default
    if isinstance(default, int):
        return default + 1
    if isinstance(default, float):
        return default + 0.5
    if isinstance(default, str):
        return "__drift__"
    if isinstance(default, list):
        return ["__drift__"]
    if isinstance(default, dict):
        return {"__drift__": 1}
    # default is None (e.g. nullable sampler knobs) — pick by declared UI type.
    return 1 if field.type == "number" else "__drift__"


def _nest(dotted: str, value: object) -> dict:
    """Build the minimal nested config dict setting ``dotted`` to ``value``."""
    d: dict = {}
    cursor = d
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return d


def _from_dict_consumes(field) -> tuple[object, object]:
    """Run from_dict over a sentinel-bearing dict; return (sentinel, parsed)."""
    sentinel = _sentinel_for(field)
    cfg = LangGraphConfig.from_dict(_nest(field.key, sentinel))
    return sentinel, getattr(cfg, field.attr)


def test_from_dict_unconsumed_set_is_exactly_as_expected():
    """A non-default sentinel placed at each non-secret dotted YAML key must land
    on the mapped dataclass attr after ``from_dict``. The live set of keys
    from_dict drops must equal the documented exception set, so a new field
    can't silently join it and a stale exception can't linger; each drifted key
    is named with its parsed vs sentinel value.

    Secrets are excluded: their YAML value competes with the secrets-overlay
    path and config_to_dict redacts them anyway, so the sentinel contract
    doesn't hold (and #2 already excludes them for the same reason)."""
    drift = {}
    for f in _NON_SECRET_FIELDS:
        sentinel, parsed = _from_dict_consumes(f)
        if parsed != sentinel:
            drift[f.key] = f"LangGraphConfig.{f.attr}={parsed!r}, expected sentinel {sentinel!r}"
    live_unconsumed = set(drift)
    assert live_unconsumed == FROM_DICT_UNCONSUMED_KEYS, (
        f"from_dict consumption drift: {drift} "
        f"(expected exceptions {FROM_DICT_UNCONSUMED_KEYS}). Add the missing parse line in "
        "LangGraphConfig.from_dict, or document the exception here with a reason."
    )
