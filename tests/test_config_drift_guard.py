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

KNOWN GAPS (documented as ``strict=True`` xfails so they auto-flip to a LOUD
failure the moment the refactor closes them):

* ``identity.org`` (attr ``identity_org``) — RESOLVED in PR-2: the dataclass field
  + the ``from_yaml`` parse line were added, so the white-label org label persists
  now. ``ATTR_MISSING_KEYS`` is empty and test #1's ``identity.org`` param passes
  normally (the strict xfail is gone).
* ``config_to_dict`` was PARTIAL — RESOLVED in PR-3: it is now FIELDS-driven and
  serializes ALL FIELDS keys (the 27 previously-missing keys — routing, compaction,
  goal, execute_code, prompt_cache, several checkpoint/knowledge/telemetry keys,
  agent_runtime, operator_mcp.tools, middleware.enforcement — are now emitted).
  ``CONFIG_TO_DICT_MISSING_KEYS`` is therefore empty, the per-key strict xfails in
  test #2 are gone (every key passes normally), and
  ``test_config_to_dict_missing_set_is_exactly_as_expected`` asserts it stays empty.
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.config_io import config_to_dict
from graph.settings_schema import FIELDS

# --------------------------------------------------------------------------- #
# Known-broken sets (the ONLY currently-failing parts — everything else passes
# normally). Keep these tight: a marker on a part that actually holds would mask
# real, future drift.
# --------------------------------------------------------------------------- #

# B1: FIELDS entries whose .attr is absent on LangGraphConfig. Now EMPTY —
# identity.org was the only one, and PR-2 added the `identity_org` dataclass
# field + from_yaml parse line, so every FIELDS attr now exists. The per-field
# strict xfail is therefore gone (identity.org passes normally), and
# test_no_unexpected_attr_drift asserts this stays empty.
ATTR_MISSING_KEYS: set[str] = set()

# B1: non-secret FIELDS keys that config_to_dict does NOT serialize. RESOLVED in
# PR-3 — config_to_dict is now FIELDS-driven and emits every FIELDS key, so this
# is EMPTY. The per-field strict xfail in test #2 is therefore gone (all keys pass
# normally), and test_config_to_dict_missing_set_is_exactly_as_expected asserts
# this set stays empty so any future serializer regression fails loudly.
CONFIG_TO_DICT_MISSING_KEYS: set[str] = set()

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


def _id(f) -> str:
    return f.key


def _param(field, known_broken: set, reason: str):
    """Wrap a Field in a pytest param, attaching a STRICT xfail marker iff the
    field is in the documented known-broken set.

    ``strict=True`` is the whole point: while the gap is open the test xfails
    (suite stays green); the instant the refactor closes the gap the assertion
    passes, the strict xfail turns that XPASS into a FAILURE, forcing whoever
    fixed it to delete the now-stale marker. Parts that already hold get NO
    marker, so real future drift fails normally.
    """
    marks = [pytest.mark.xfail(reason=reason, strict=True)] if field.key in known_broken else []
    return pytest.param(field, id=field.key, marks=marks)


_ATTR_REASON = (
    "B1: FIELDS key maps to a LangGraphConfig attribute that does not exist "
    "(no identity_org field / from_yaml parse). Fixed when identity_org is added "
    "to the dataclass."
)
_SERIALIZE_REASON = (
    "B1: config_to_dict does not serialize this key (partial serializer — omits "
    "routing/compaction/goal/execute_code/prompt_cache/checkpoint/telemetry/"
    "agent_runtime/operator_mcp/middleware.enforcement/knowledge.embeddings+facts). "
    "Fixed when config_to_dict becomes FIELDS-complete."
)


# --------------------------------------------------------------------------- #
# 1) Every FIELDS.attr exists on the dataclass.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field", [_param(f, ATTR_MISSING_KEYS, _ATTR_REASON) for f in FIELDS]
)
def test_every_fields_attr_exists_on_dataclass(field):
    """Each settings Field must point at a real ``LangGraphConfig`` attribute.

    A missing attr means the Settings UI renders a control that reads
    ``None``/default and whose save never round-trips into the live config.
    """
    cfg = LangGraphConfig()
    assert hasattr(cfg, field.attr), (
        f"FIELDS key {field.key!r} maps to LangGraphConfig.{field.attr}, "
        f"but that attribute does not exist on the dataclass — settings drift."
    )


def test_attr_missing_set_is_exactly_as_expected():
    """Belt-and-suspenders: the live missing-attr set equals the documented one.

    If a NEW attr goes missing it lands here (not in the silently-xfailed
    per-field param), so unexpected drift fails loudly instead of being absorbed
    by the known-gap marker.
    """
    cfg = LangGraphConfig()
    live_missing = {f.key for f in FIELDS if not hasattr(cfg, f.attr)}
    assert live_missing == ATTR_MISSING_KEYS, (
        f"missing-attr drift changed: {live_missing} (expected {ATTR_MISSING_KEYS}). "
        "Add/remove the offending FIELDS key from ATTR_MISSING_KEYS, or fix the dataclass."
    )


# --------------------------------------------------------------------------- #
# 2) Every non-secret FIELDS key resolves in config_to_dict's output.
# --------------------------------------------------------------------------- #

_NON_SECRET_FIELDS = [f for f in FIELDS if f.key not in _SECRET_KEYS]


@pytest.mark.parametrize(
    "field",
    [_param(f, CONFIG_TO_DICT_MISSING_KEYS, _SERIALIZE_REASON) for f in _NON_SECRET_FIELDS],
)
def test_fields_keys_present_in_config_to_dict(field):
    """Each non-secret settings key must round-trip through ``config_to_dict``.

    Secrets are excluded (config_to_dict deliberately redacts them, and may
    omit a never-set section). A key that the serializer drops means a Settings
    save can be silently lost on the next load.
    """
    d = config_to_dict(LangGraphConfig())
    found, _ = _resolve(d, field.key)
    assert found, (
        f"FIELDS key {field.key!r} does not resolve in config_to_dict() output "
        f"(top-level keys: {sorted(d.keys())}) — serializer drift."
    )


def test_config_to_dict_missing_set_is_exactly_as_expected():
    """The live set of non-secret keys config_to_dict omits == the documented set.

    A newly-dropped key surfaces here (loud) rather than being silently absorbed
    by a stale xfail marker; a newly-covered key also flips the per-field strict
    xfail, so this stays in agreement with reality both ways.
    """
    d = config_to_dict(LangGraphConfig())
    live_missing = {f.key for f in _NON_SECRET_FIELDS if not _resolve(d, f.key)[0]}
    assert live_missing == CONFIG_TO_DICT_MISSING_KEYS, (
        f"config_to_dict coverage drift: {live_missing} "
        f"(expected {CONFIG_TO_DICT_MISSING_KEYS}). Update CONFIG_TO_DICT_MISSING_KEYS "
        "or expand config_to_dict."
    )


# --------------------------------------------------------------------------- #
# 3) Field.type agrees with the dataclass default's Python type (best-effort).
# --------------------------------------------------------------------------- #

# Fields without a backing attr can't have their value's type checked here —
# that drift is owned by test #1.
_TYPED_FIELDS = [f for f in FIELDS if f.key not in ATTR_MISSING_KEYS]


@pytest.mark.parametrize("field", _TYPED_FIELDS, ids=_id)
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
    cfg = LangGraphConfig()
    val = getattr(cfg, field.attr)
    if field.type == "bool":
        assert isinstance(val, bool), (
            f"{field.key!r} is type 'bool' but {field.attr} defaults to "
            f"{type(val).__name__} ({val!r})."
        )
    elif field.type == "number":
        assert isinstance(val, (int, float)) and not isinstance(val, bool), (
            f"{field.key!r} is type 'number' but {field.attr} defaults to "
            f"{type(val).__name__} ({val!r})."
        )
    elif field.type == "string_list":
        assert isinstance(val, list), (
            f"{field.key!r} is type 'string_list' but {field.attr} defaults to "
            f"{type(val).__name__} ({val!r})."
        )


# --------------------------------------------------------------------------- #
# 4) Field.scope assignment (ADR 0047 §2.1 Decision 2) — the box-shared set.
# --------------------------------------------------------------------------- #

# The fields whose shared default lives at the HOST layer (gateway/model/routing/
# cache/telemetry infra + org branding). Everything else is "agent". Locked here so
# a new Field can't silently land in the wrong cascade layer.
HOST_SCOPED_KEYS = {
    "model.api_base", "model.provider", "model.name",
    "routing.aux_model", "routing.fallback_models",
    "prompt_cache.enabled", "prompt_cache.ttl",
    "prompt_cache.warm.enabled", "prompt_cache.warm.interval_seconds",
    "telemetry.enabled", "telemetry.retention_days",
    "identity.org",
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

_FROM_DICT_REASON = (
    "from_dict has no parse line for this FIELDS key — the YAML value is "
    "silently ignored and the dataclass default wins. Add the kwarg line in "
    "LangGraphConfig.from_dict."
)


def _sentinel_for(field) -> object:
    """A guaranteed NON-default value of the right shape for ``field``.

    Derived from the dataclass default's Python type (bool checked before int —
    it's an int subclass), falling back to the Field's declared UI type when the
    default is ``None``. Using the default as the base means the sentinel can
    never accidentally equal it.
    """
    default = getattr(LangGraphConfig(), field.attr)
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


@pytest.mark.parametrize(
    "field",
    [_param(f, FROM_DICT_UNCONSUMED_KEYS, _FROM_DICT_REASON) for f in _NON_SECRET_FIELDS],
)
def test_from_dict_consumes_every_fields_key(field):
    """A non-default sentinel placed at the dotted YAML key must land on the
    mapped dataclass attr after ``from_dict``.

    Secrets are excluded: their YAML value competes with the secrets-overlay
    path and config_to_dict redacts them anyway, so the sentinel contract
    doesn't hold (and #2 already excludes them for the same reason).
    """
    sentinel, parsed = _from_dict_consumes(field)
    assert parsed == sentinel, (
        f"FIELDS key {field.key!r}: from_dict left LangGraphConfig.{field.attr} "
        f"at {parsed!r} instead of the sentinel {sentinel!r} — the parse line "
        "is missing or reads a different key."
    )


def test_from_dict_unconsumed_set_is_exactly_as_expected():
    """Belt-and-suspenders (same pattern as tests #1/#2): the live set of
    FIELDS keys from_dict drops equals the documented exception set, so a new
    field can't silently join it and a stale exception can't linger."""
    live_unconsumed = set()
    for f in _NON_SECRET_FIELDS:
        sentinel, parsed = _from_dict_consumes(f)
        if parsed != sentinel:
            live_unconsumed.add(f.key)
    assert live_unconsumed == FROM_DICT_UNCONSUMED_KEYS, (
        f"from_dict consumption drift: {live_unconsumed} "
        f"(expected {FROM_DICT_UNCONSUMED_KEYS}). Add the missing parse line in "
        "LangGraphConfig.from_dict, or document the exception here with a reason."
    )
