"""ADR 0108 D9 — the prior-session digest is evaluated, not blindly injected.

Covers the three ``context.prior_sessions`` policies (newest / relevant / off),
the active-session exclusion (a thread's own summary must never be injected as a
"prior" session — it is the NEWEST file on disk from turn 2 on), and the
per-entry budget shed the D6 engine deferred to D9. The D8 golden stays
byte-identical: under ``newest`` with no shed the loader's pre-rendered block
passes through verbatim (tests/test_projection.py proves it).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph.middleware.memory import (
    _DIGEST_HEADER,
    DigestEntry,
    DigestResult,
    finish_digest,
    load_digest,
    load_digest_pool,
    load_prior_sessions_digest,
    render_digest,
)
from graph.projection import ProjectionOptions, compose_projected_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _summary(session_id: str, topic: str, *, gist: str = "") -> dict:
    summary = {
        "session_id": session_id,
        "trace_id": f"trace-{session_id}",
        "messages": [{"role": "user", "content": topic}],
        "final_output": f"done: {topic}",
        "timestamp": "2026-08-28T12:00:00+00:00",
    }
    if gist:
        summary["gist"] = gist
    return summary


def _write(memory_dir: Path, session_id: str, topic: str, *, mtime: float | None = None, gist: str = "") -> None:
    from graph.middleware.memory import session_filename

    path = memory_dir / session_filename(session_id)
    path.write_text(json.dumps(_summary(session_id, topic, gist=gist)), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _entries(n: int) -> list[DigestEntry]:
    return [DigestEntry(f"s{i}", f"  s{i} · 2026-08-2{i} · chat · topic {i} · 1 msgs") for i in range(1, n + 1)]


def _quiet_working_state(monkeypatch) -> None:
    monkeypatch.setattr("graph.projection.working_state_block", lambda state: "")


# ---------------------------------------------------------------------------
# Active-session exclusion (the confirmed leak)
# ---------------------------------------------------------------------------


def test_loader_excludes_the_active_session_and_refills(tmp_path):
    """exclude_session_id skips the active session BEFORE the newest-N cut, so
    the pool refills with the next-newest instead of running one short."""
    now = time.time()
    _write(tmp_path, "active", "the current conversation", mtime=now)
    _write(tmp_path, "older-1", "release prep", mtime=now - 60)
    _write(tmp_path, "older-2", "budget review", mtime=now - 120)

    block, ids = load_prior_sessions_digest(str(tmp_path), max_sessions=2, exclude_session_id="active")
    assert "active" not in block
    assert ids == ["older-1", "older-2"]  # refilled to the full N, newest first

    # Without the exclusion the active session IS the newest "prior" entry —
    # the leak this decision exists to close.
    block_leaky, ids_leaky = load_prior_sessions_digest(str(tmp_path), max_sessions=2)
    assert ids_leaky[0] == "active" and "active" in block_leaky


def test_pool_excludes_by_content_id_even_when_the_filename_differs(tmp_path):
    """A summary whose filename doesn't match its content id is still excluded
    by the post-parse check."""
    now = time.time()
    _write(tmp_path, "disguised", "whatever", mtime=now)
    # Rewrite the content id to the active session's, leaving the filename alone.
    from graph.middleware.memory import session_filename

    p = tmp_path / session_filename("disguised")
    doc = json.loads(p.read_text())
    doc["session_id"] = "the-active-one"
    p.write_text(json.dumps(doc))
    _write(tmp_path, "other", "release prep", mtime=now - 60)

    pool, exists = load_digest_pool(str(tmp_path), 10, exclude_session_id="the-active-one")
    assert exists and [e.session_id for e in pool] == ["other"]


def test_mid_thread_leak_regression_via_the_middleware(tmp_path):
    """From turn 2 a session's own summary is the newest file on disk; the
    composer must pass the active sid through so the middleware's pooled digest
    never lists it."""
    from langchain_core.messages import HumanMessage

    from graph.middleware.knowledge import KnowledgeMiddleware

    now = time.time()
    _write(tmp_path, "this-thread", "what we are doing right now", mtime=now)
    _write(tmp_path, "yesterday", "release prep", mtime=now - 60)

    mw = KnowledgeMiddleware(None)
    mw._prior_sessions_cache = mw.load_memory(memory_path=str(tmp_path))  # primes the POOL too
    mw._prior_sessions_loaded_at = time.monotonic()

    composed = mw.compose_context(
        {"messages": [HumanMessage(content="hello")], "session_id": "this-thread"}, record=False
    )
    ctx = (composed or {}).get("context") or ""
    assert "yesterday" in ctx
    assert "this-thread" not in ctx  # its own summary is not a "prior" session

    # A different session sharing the same middleware still sees this-thread's
    # summary — the exclusion is per call, not baked into the cache.
    composed_other = mw.compose_context(
        {"messages": [HumanMessage(content="hello")], "session_id": "someone-else"}, record=False
    )
    assert "this-thread" in ((composed_other or {}).get("context") or "")


# ---------------------------------------------------------------------------
# The `relevant` policy
# ---------------------------------------------------------------------------


def _fts_available(tmp_path) -> bool:
    try:
        from graph.session_search import search_session_summaries

        search_session_summaries("probe", memory_dir=str(tmp_path), limit=1)
        return True
    except Exception:  # noqa: BLE001 — no FTS5 in this build
        return False


def test_relevant_injects_only_matching_sessions_in_rank_order(tmp_path):
    now = time.time()
    _write(tmp_path, "rockets", "planning the rocket launch window", mtime=now - 30)
    _write(tmp_path, "gardening", "watering the tomato plants", mtime=now - 60)
    _write(tmp_path, "newest-noise", "quarterly budget spreadsheet", mtime=now)
    if not _fts_available(tmp_path):
        pytest.skip("SQLite FTS5 unavailable")

    res = load_digest("relevant", query="rocket launch", memory_dir=str(tmp_path))
    assert [e.session_id for e in res.entries] == ["rockets"]
    assert "rockets" in res.block and "gardening" not in res.block and "newest-noise" not in res.block

    # Deterministic across runs.
    res2 = load_digest("relevant", query="rocket launch", memory_dir=str(tmp_path))
    assert res2 == res


def test_relevant_falls_back_to_newest_on_empty_query_zero_matches_or_no_index(tmp_path, monkeypatch):
    now = time.time()
    _write(tmp_path, "alpha", "release prep", mtime=now)
    _write(tmp_path, "beta", "budget review", mtime=now - 60)

    newest = load_digest("newest", memory_dir=str(tmp_path))
    assert [e.session_id for e in newest.entries] == ["alpha", "beta"]

    # Empty / whitespace query → newest without touching the index.
    assert load_digest("relevant", query="   ", memory_dir=str(tmp_path)) == newest

    # Index unavailable → newest.
    import graph.session_search as ss

    def _boom(*a, **k):
        raise ss.SessionSearchUnavailable("no fts5")

    monkeypatch.setattr(ss, "search_session_summaries", _boom)
    assert load_digest("relevant", query="release", memory_dir=str(tmp_path)) == newest

    # Zero matches → newest.
    monkeypatch.setattr(ss, "search_session_summaries", lambda *a, **k: [])
    assert load_digest("relevant", query="zzzunmatchable", memory_dir=str(tmp_path)) == newest


def test_relevant_excludes_the_active_session_in_the_fallback_too(tmp_path, monkeypatch):
    now = time.time()
    _write(tmp_path, "me", "the current thread", mtime=now)
    _write(tmp_path, "other", "release prep", mtime=now - 60)
    import graph.session_search as ss

    monkeypatch.setattr(ss, "search_session_summaries", lambda *a, **k: [])
    res = load_digest("relevant", query="release", exclude_session_id="me", memory_dir=str(tmp_path))
    assert [e.session_id for e in res.entries] == ["other"]


# ---------------------------------------------------------------------------
# The `off` policy + suppressions
# ---------------------------------------------------------------------------


def _spy_loader():
    calls = {"n": 0}

    def loader(**kw):
        calls["n"] += 1
        return DigestResult(render_digest(_entries(2)), _entries(2))

    return loader, calls


@pytest.mark.parametrize("policy", ["newest", "relevant", "off"])
def test_incognito_and_goal_turns_never_invoke_the_loader(monkeypatch, policy):
    _quiet_working_state(monkeypatch)
    loader, calls = _spy_loader()
    opts = ProjectionOptions(prior_sessions_policy=policy)

    p = compose_projected_context(
        "q", None, None, {}, incognito=True, record=False, options=opts, prior_sessions=loader
    )
    assert calls["n"] == 0 and "prior_sessions" not in p.sources

    monkeypatch.setattr("graph.projection._in_goal_turn", lambda: True)
    p = compose_projected_context("q", None, None, {}, record=False, options=opts, prior_sessions=loader)
    assert calls["n"] == 0 and "prior_sessions" not in p.sources


def test_off_never_invokes_the_loader_and_emits_no_source(monkeypatch):
    _quiet_working_state(monkeypatch)
    loader, calls = _spy_loader()
    p = compose_projected_context(
        "q",
        None,
        None,
        {},
        record=False,
        options=ProjectionOptions(prior_sessions_policy="off"),
        prior_sessions=loader,
    )
    assert calls["n"] == 0
    assert "prior_sessions" not in p.sources and "<prior_sessions" not in p.text


def test_newest_default_invokes_the_loader_with_query_and_active_sid(monkeypatch):
    _quiet_working_state(monkeypatch)
    seen = {}

    def loader(**kw):
        seen.update(kw)
        return DigestResult(render_digest(_entries(1)), _entries(1))

    p = compose_projected_context(
        "what changed?",
        None,
        None,
        {"session_id": "sess-42"},
        record=False,
        options=ProjectionOptions(),
        prior_sessions=loader,
    )
    assert seen == {"query": "what changed?", "exclude_session_id": "sess-42"}
    assert p.digest_ids == ["s1"] and "prior_sessions" in p.sources


# ---------------------------------------------------------------------------
# Per-entry budget shed (D6 deferred this to D9)
# ---------------------------------------------------------------------------


def test_budget_sheds_digest_entries_from_the_end_then_the_section(monkeypatch):
    _quiet_working_state(monkeypatch)
    entries = _entries(3)
    loader = lambda **kw: DigestResult(render_digest(entries), list(entries))  # noqa: E731

    full = compose_projected_context(
        "", None, None, {}, record=False, options=ProjectionOptions(), prior_sessions=loader
    )
    assert full.digest_ids == ["s1", "s2", "s3"]

    two = render_digest(entries[:2])
    p = compose_projected_context(
        "",
        None,
        None,
        {},
        record=False,
        options=ProjectionOptions(budget_chars=len(full.text) - 1),
        prior_sessions=loader,
    )
    # Oldest (the END of keep-first ordering) dropped first; the header survives.
    assert p.digest_ids == ["s1", "s2"]
    assert "s3 ·" not in p.text
    assert two in p.text and _DIGEST_HEADER in p.text  # re-rendered block, same bytes as the loader's
    assert p.overflow == [
        {"label": "Prior sessions", "dropped_items": 1, "dropped_chars": len(full.text) - len(p.text)}
    ]
    assert p.sections and p.sections[0].get("truncated") is True

    # A budget below every entry drops the whole section.
    tiny = compose_projected_context(
        "",
        None,
        None,
        {},
        record=False,
        options=ProjectionOptions(budget_chars=1),
        prior_sessions=loader,
    )
    assert tiny.digest_ids == [] and "<prior_sessions" not in tiny.text
    assert tiny.overflow[0]["label"] == "Prior sessions" and tiny.overflow[0]["dropped_items"] == 3


def test_legacy_two_tuple_loader_still_sheds_as_one_unit(monkeypatch):
    _quiet_working_state(monkeypatch)
    block = render_digest(_entries(3))
    loader = lambda: (block, ["s1", "s2", "s3"])  # noqa: E731 — zero-arg legacy shape

    full = compose_projected_context(
        "", None, None, {}, record=False, options=ProjectionOptions(), prior_sessions=loader
    )
    assert full.digest_ids == ["s1", "s2", "s3"]
    p = compose_projected_context(
        "",
        None,
        None,
        {},
        record=False,
        options=ProjectionOptions(budget_chars=len(full.text) - 1),
        prior_sessions=loader,
    )
    assert p.digest_ids == [] and "<prior_sessions" not in p.text
    assert p.overflow[0] == {
        "label": "Prior sessions",
        "dropped_items": 3,
        "dropped_chars": len(full.text) - len(p.text),
    }


def test_render_digest_round_trips_the_loader_bytes(tmp_path):
    _write(tmp_path, "a", "release prep", mtime=time.time())
    res = load_digest("newest", memory_dir=str(tmp_path))
    assert res.block == render_digest(res.entries)
    assert render_digest([]) == ""
    assert finish_digest([], dir_exists=False) == DigestResult("", [])
    assert finish_digest([], dir_exists=True) == DigestResult("<prior_sessions/>", [])


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_config_coerces_the_policy_and_warns_on_garbage(caplog):
    """The config layer normalizes + warns but passes an unknown value through
    (the drift guard's consumption contract); the PROJECTION layer is where an
    unknown value reads as ``newest`` — asserted in the from_config test below."""
    from graph.config import _coerce_prior_sessions

    assert _coerce_prior_sessions(" Relevant ", "newest") == "relevant"
    assert _coerce_prior_sessions("off", "newest") == "off"
    assert _coerce_prior_sessions("", "newest") == "newest"
    with caplog.at_level("WARNING"):
        assert _coerce_prior_sessions("sometimes", "newest") == "sometimes"
    assert any("prior_sessions" in r.message for r in caplog.records)


def test_bare_off_in_yaml_turns_the_digest_off(caplog):
    """`prior_sessions: off` written the documented way, parsed the way YAML
    actually parses it.

    YAML 1.1 reads a bare ``off`` as the BOOLEAN False (so do ``no`` and ``false``),
    and this knob's off switch is spelled exactly that. Before the fix, False fell
    through ``value or ""`` as blank and resolved to the DEFAULT — ``newest``, the
    most expensive setting, the exact opposite of the request, silently. Live on the
    dev instance that delivered a 10-session digest to an operator who had asked for
    none. The test above passes the STRING "off" and so could never see it; these go
    through the parser.
    """
    import yaml

    from graph.config import _coerce_prior_sessions

    for spelling in ("off", "no", "false"):
        parsed = yaml.safe_load(f"prior_sessions: {spelling}")["prior_sessions"]
        assert parsed is False, f"YAML changed: bare {spelling} no longer parses as False"
        assert _coerce_prior_sessions(parsed, "newest") == "off"

    # Quoted stays a plain string, and the other direction is not a value this knob
    # has — `on`/`yes` warn and take the default rather than inventing "true".
    assert _coerce_prior_sessions(yaml.safe_load("v: 'off'")["v"], "newest") == "off"
    with caplog.at_level("WARNING"):
        assert _coerce_prior_sessions(yaml.safe_load("v: on")["v"], "newest") == "newest"
    assert any("prior_sessions" in r.message for r in caplog.records)


def test_bare_off_reaches_the_projection_as_off():
    """End of the wiring: a False from YAML must arrive at the projection as the
    off policy, not merely be tidied up inside the config layer."""
    assert ProjectionOptions.from_config(SimpleNamespace(context_prior_sessions=False)).prior_sessions_policy == "off"


def test_from_config_reads_and_normalizes_the_policy():
    assert (
        ProjectionOptions.from_config(SimpleNamespace(context_prior_sessions=" RELEVANT ")).prior_sessions_policy
        == "relevant"
    )
    assert (
        ProjectionOptions.from_config(SimpleNamespace(context_prior_sessions="bogus")).prior_sessions_policy == "newest"
    )
    assert ProjectionOptions.from_config(SimpleNamespace()).prior_sessions_policy == "newest"


def test_yaml_section_maps_prior_sessions(tmp_path):
    from graph.config import LangGraphConfig

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("context:\n  prior_sessions: relevant\n", encoding="utf-8")
    cfg = LangGraphConfig.from_yaml(str(cfg_path))
    assert cfg.context_prior_sessions == "relevant"


# ---------------------------------------------------------------------------
# Review round: pool refill on the production path (B2) + hardening
# ---------------------------------------------------------------------------


def test_middleware_digest_refills_when_the_active_session_is_dropped(tmp_path):
    """The shared TTL pool holds one MORE entry than the digest shows, and the
    trim runs AFTER the per-call exclusion — so a session whose own summary is
    dropped still gets a full-length digest instead of one entry short."""
    from langchain_core.messages import HumanMessage

    from graph.middleware.knowledge import KnowledgeMiddleware

    now = time.time()
    # 11 summaries: "newest" is the active session, then s01 (newest) … s10.
    _write(tmp_path, "active", "the current thread", mtime=now)
    for i in range(1, 11):
        _write(tmp_path, f"s{i:02d}", f"topic {i}", mtime=now - i * 60)

    mw = KnowledgeMiddleware(None)
    mw._prior_sessions_cache = mw.load_memory(memory_path=str(tmp_path))
    mw._prior_sessions_loaded_at = time.monotonic()

    def _ids(session_id: str) -> list[str]:
        mw.compose_context({"messages": [HumanMessage(content="hi")], "session_id": session_id}, record=False)
        return list(mw._prior_sessions_ids)

    mine = _ids("active")
    assert "active" not in mine
    assert len(mine) == 10, f"digest ran short: {mine}"
    assert "s10" in mine  # the spare refilled the slot the exclusion freed

    # A session that isn't in the pool sees the plain newest-10 (active included).
    other = _ids("someone-else")
    assert other[0] == "active" and len(other) == 10 and "s10" not in other


def test_modern_loader_raising_typeerror_is_not_retried_bare(monkeypatch):
    """A TypeError raised INSIDE a modern loader must not trigger the legacy
    bare-call retry — that would ship a digest with the exclusion silently
    dropped. The kwargs-vs-bare decision comes from the signature."""
    _quiet_working_state(monkeypatch)
    calls = {"n": 0}

    def loader(*, query="", exclude_session_id=""):
        calls["n"] += 1
        raise TypeError("boom inside the loader")

    p = compose_projected_context("q", None, None, {}, record=False, options=ProjectionOptions(), prior_sessions=loader)
    assert calls["n"] == 1  # invoked once, not retried
    assert p.digest_ids == [] and "<prior_sessions" not in p.text


def test_directly_constructed_options_normalize_the_policy():
    """``ProjectionOptions(prior_sessions_policy="OFF")`` must mean what
    from_config makes of it, not silently newest."""
    assert ProjectionOptions(prior_sessions_policy="OFF").prior_sessions_policy == "off"
    assert ProjectionOptions(prior_sessions_policy=" Relevant ").prior_sessions_policy == "relevant"
    assert ProjectionOptions(prior_sessions_policy="bogus").prior_sessions_policy == "newest"


def test_off_policy_holds_when_constructed_uppercase(monkeypatch):
    _quiet_working_state(monkeypatch)
    loader, calls = _spy_loader()
    compose_projected_context(
        "q",
        None,
        None,
        {},
        record=False,
        options=ProjectionOptions(prior_sessions_policy="OFF"),
        prior_sessions=loader,
    )
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# The digest ceilings (`memory.max_sessions` / `memory.max_tokens`, #3308)
# ---------------------------------------------------------------------------


def test_yaml_ceilings_reach_the_projection():
    """The two knobs were documented in the example config and read by NOTHING
    for their whole life — an operator shrinking the digest got a silent no-op."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig.from_dict({"memory": {"max_sessions": 3, "max_tokens": 900}})
    assert (cfg.memory_max_sessions, cfg.memory_max_tokens) == (3, 900)
    opts = ProjectionOptions.from_config(cfg)
    assert (opts.prior_sessions_max, opts.prior_sessions_max_tokens) == (3, 900)


@pytest.mark.parametrize("bad", [0, -5, "", "many", None])
def test_non_positive_ceilings_fall_back_to_the_default(bad, caplog):
    """A ceiling is not a switch: 0 would be an undocumented second spelling of
    `context.prior_sessions: off`, and it must not arrive there silently."""
    from graph.config import LangGraphConfig

    with caplog.at_level("WARNING"):
        cfg = LangGraphConfig.from_dict({"memory": {"max_sessions": bad}})
    assert cfg.memory_max_sessions == 10
    assert any("memory.max_sessions" in r.getMessage() for r in caplog.records)
    # Directly-constructed options agree with what from_config makes of it.
    assert ProjectionOptions(prior_sessions_max=0).prior_sessions_max == 10
    assert ProjectionOptions(prior_sessions_max_tokens=-1).prior_sessions_max_tokens == 2000


def test_middleware_digest_lists_only_the_configured_count(tmp_path):
    from graph.middleware.knowledge import KnowledgeMiddleware

    now = time.time()
    for i in range(5):
        _write(tmp_path, f"s{i}", f"topic {i}", mtime=now - i)
    mw = KnowledgeMiddleware(None, options=ProjectionOptions(prior_sessions_max=2))
    block = mw.load_memory(memory_path=str(tmp_path))
    assert "s0" in block and "s1" in block
    assert "s2" not in block and "s4" not in block


def test_token_ceiling_trims_the_block(tmp_path):
    now = time.time()
    for i in range(5):
        _write(tmp_path, f"s{i}", f"topic {i}", mtime=now - i)
    from graph.middleware.knowledge import KnowledgeMiddleware

    full = KnowledgeMiddleware(None).load_memory(memory_path=str(tmp_path))
    # Room for the framing header plus about one entry line.
    cap = max(1, (len(_DIGEST_HEADER) + 80) // 4)
    trimmed = KnowledgeMiddleware(None, options=ProjectionOptions(prior_sessions_max_tokens=cap)).load_memory(
        memory_path=str(tmp_path)
    )
    # finish_digest measures the header + entry lines (not the wrapper tags) — the
    # same char/4 estimate the cap has always been expressed in.
    assert len("\n".join(trimmed.splitlines()[1:-1])) // 4 <= cap
    assert trimmed.count("\n") < full.count("\n")  # entries were shed
    assert "s0" in trimmed and "s4" not in trimmed  # newest kept, oldest shed first


def test_load_memory_excludes_the_caller_on_the_public_seam(tmp_path):
    """`load_memory` is public — a fork or plugin calling it directly must get the
    same guarantee the composer has, not the leak #3252 fixed."""
    from graph.middleware.knowledge import KnowledgeMiddleware

    now = time.time()
    _write(tmp_path, "mine", "what I am doing now", mtime=now)
    _write(tmp_path, "theirs", "release prep", mtime=now - 60)
    mw = KnowledgeMiddleware(None)
    block = mw.load_memory(memory_path=str(tmp_path), exclude_session_id="mine")
    assert "theirs" in block and "mine" not in block
    assert mw._prior_sessions_ids == ["theirs"]
    # The stashed POOL stays unfiltered: it is shared across sessions, so baking
    # one caller's exclusion into it would hide "mine" from every other thread.
    assert {e.session_id for e in mw._prior_sessions_pool} == {"mine", "theirs"}


def test_raising_the_ceiling_reloads_the_pool_inside_the_ttl(tmp_path, monkeypatch):
    """The pool is READ at max_sessions + 1, so a widened ceiling cannot be served
    by trimming what is already cached — it needs the wider disk read."""
    from graph.middleware.knowledge import KnowledgeMiddleware

    now = time.time()
    for i in range(5):
        _write(tmp_path, f"s{i}", f"topic {i}", mtime=now - i)
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))  # memory_path() resolves per call

    mw = KnowledgeMiddleware(None, options=ProjectionOptions(prior_sessions_max=2))
    first = mw._cached_digest()
    assert [e.session_id for e in first.entries] == ["s0", "s1"]

    mw._options_override = ProjectionOptions(prior_sessions_max=4)
    second = mw._cached_digest()  # same TTL window, wider ceiling
    assert [e.session_id for e in second.entries] == ["s0", "s1", "s2", "s3"]


def test_load_memory_defaults_the_exclusion_to_the_ambient_session(tmp_path, monkeypatch):
    """Bare `load_memory()` mid-turn must not hand back the caller's own summary:
    the default reads the tracing session id, the same chain the summary WRITER
    keys on. `""` is the explicit opt-out the shared cache primer uses."""
    from observability import tracing

    from graph.middleware.knowledge import KnowledgeMiddleware

    now = time.time()
    _write(tmp_path, "mine", "what I am doing now", mtime=now)
    _write(tmp_path, "theirs", "release prep", mtime=now - 60)
    mw = KnowledgeMiddleware(None)

    monkeypatch.setattr(tracing, "current_session_id", lambda: "mine")
    assert "mine" not in mw.load_memory(memory_path=str(tmp_path))
    assert "mine" in mw.load_memory(memory_path=str(tmp_path), exclude_session_id="")


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("policy", ["newest", "relevant"])
@pytest.mark.parametrize("legacy_names", [False, True])
def test_automated_sessions_do_not_take_chat_digest_slots(tmp_path, policy, legacy_names):
    from graph.middleware.memory import session_filename
    from graph.session_search import search_session_summaries

    for i in range(3):
        _write(tmp_path, f"chat-{i}", "rocket launch", mtime=100 + i)
    _write(tmp_path, "a2a:outside", "rocket launch", mtime=90)
    automated = [f"job:{i}" if i % 2 else f"palette-{i}" for i in range(10)]
    automated += ["system:activity", "background:legacy"]
    for i, sid in enumerate(automated):
        _write(tmp_path, sid, "rocket launch", mtime=200 + i)
        # A raw `job:1.json` cannot exist on Windows (":" is not a legal filename
        # character, WinError 123), so no legacy file there carries a colon id; the
        # colon-free ids (`palette-*`) still exercise the legacy names on every OS.
        if legacy_names and (os.name != "nt" or ":" not in sid):
            (tmp_path / session_filename(sid)).rename(tmp_path / f"{sid}.json")
    if policy == "relevant" and not _fts_available(tmp_path):
        pytest.skip("SQLite FTS5 unavailable")

    result = load_digest(policy, query="rocket launch", memory_dir=str(tmp_path))
    assert {e.session_id for e in result.entries} == {"chat-0", "chat-1", "chat-2", "a2a:outside"}
    assert all(sid not in result.block for sid in automated)
    if policy == "newest":
        assert [e.session_id for e in result.entries] == ["chat-2", "chat-1", "chat-0", "a2a:outside"]
    else:
        # Explicit search still exposes durable automation summaries.
        hits = search_session_summaries("rocket launch", memory_dir=str(tmp_path), surface="activity")
        assert [hit["session_id"] for hit in hits] == ["system:activity"]


def test_surface_gating_leaves_the_token_budget_untouched(tmp_path):
    """ADR 0132 D3 (acceptance #2): excluding job/palette/Activity surfaces must
    not change the delivery budget. The gate drops those sessions from the newest-N
    pool BEFORE ``finish_digest`` runs, so the SAME ``max_tokens`` char/4 trim the
    loader has always applied is measured over the chats alone — the big automated
    summaries never enter the budget math, and the public loaders' default ceiling
    is still 2000."""
    import inspect

    now = time.time()
    for i in range(3):
        _write(tmp_path, f"chat-{i}", "release prep", mtime=now - i)
    # Automated summaries with bulky bodies, interleaved by mtime. If any leaked into
    # the pool it would either take a slot or eat into the token budget below.
    for i in range(6):
        _write(tmp_path, f"job:{i}", "background churn " * 60, mtime=now - 0.5 - i)
    _write(tmp_path, "system:activity", "activity noise " * 60, mtime=now + 1)

    budget = 2000
    res = load_digest("newest", memory_dir=str(tmp_path), max_tokens=budget)
    assert {e.session_id for e in res.entries} == {"chat-0", "chat-1", "chat-2"}
    # The char/4 estimate the cap is expressed in, measured on the header + entry
    # lines (not the wrapper tags) exactly as finish_digest measures it.
    assert len("\n".join(res.block.splitlines()[1:-1])) // 4 <= budget

    # The exclusion is additive: it did not move either public loader's default budget.
    assert inspect.signature(load_digest).parameters["max_tokens"].default == 2000
    assert inspect.signature(load_prior_sessions_digest).parameters["max_tokens"].default == 2000


# ---------------------------------------------------------------------------
# ADR 0132 S5 — the digest renders each chat's gist (D1)
# ---------------------------------------------------------------------------


def test_digest_renders_a_cards_gist_after_the_topic(tmp_path):
    """A card with a gist shows it right after the topic; a card without one
    renders exactly as today — the topic line, byte for byte, no gist segment."""
    now = time.time()
    _write(
        tmp_path,
        "chat-withgist",
        "plan the launch",
        gist="Rocket launch set for Friday; the range is booked and checklists are green.",
        mtime=now,
    )
    _write(tmp_path, "chat-plain", "budget review", mtime=now - 60)

    res = load_digest("newest", memory_dir=str(tmp_path))
    lines = {e.session_id: e.line for e in res.entries}

    # Gist renders in the `topic · gist · N msgs` slot.
    assert lines["chat-withgist"] == (
        "  chat-withgist · 2026-08-28T12:00:00+00:00 · chat · plan the launch · "
        "Rocket launch set for Friday; the range is booked and checklists are green. · 1 msgs"
    )
    # No gist → the pre-S5 shape unchanged.
    assert lines["chat-plain"] == "  chat-plain · 2026-08-28T12:00:00+00:00 · chat · budget review · 1 msgs"
    assert res.block.count("Rocket launch") == 1


def test_gist_is_whitespace_collapsed_and_clipped_on_the_line(tmp_path):
    """A card's gist is single-line (newlines collapsed) and bounded so one card
    cannot blow the digest budget, matching the generator's 240-char cap."""
    from graph.middleware.memory import _DIGEST_GIST_MAX_CHARS, digest_entry

    e = digest_entry(_summary("chat-x", "topic", gist="line one\n\n  line two\t" + "z" * 300))
    assert "\n" not in e["gist"] and "  " not in e["gist"]
    assert e["gist"].startswith("line one line two z")
    assert len(e["gist"]) == _DIGEST_GIST_MAX_CHARS and e["gist"].endswith("…")


def test_ten_chats_with_240_char_gists_fit_the_budget(tmp_path):
    """ADR 0132 D1 (acceptance): the digest's existing 2,000-token budget holds
    10 chats each with a full 240-char gist — no chat and no gist is dropped."""
    now = time.time()
    gist = "g" * 240
    for i in range(10):
        _write(tmp_path, f"chat-{i:02d}", f"topic {i}", gist=gist, mtime=now - i)

    res = load_digest("newest", memory_dir=str(tmp_path), max_tokens=2000)
    assert len(res.entries) == 10  # every chat kept
    assert all(gist in e.line for e in res.entries)  # every gist kept in full
    # Within budget, measured the char/4 way finish_digest measures it (header + lines).
    assert len("\n".join(res.block.splitlines()[1:-1])) // 4 <= 2000


def test_gists_are_trimmed_before_whole_chats_are_dropped(tmp_path):
    """Over budget, gists are shed (oldest first) before any chat is dropped: the
    digest stays an index, so a chat's topic line outranks another chat's gist."""
    now = time.time()
    gist = "g" * 240
    for i in range(5):  # chat-0 newest … chat-4 oldest
        _write(tmp_path, f"chat-{i}", f"topic {i}", gist=gist, mtime=now - i)

    pool, _ = load_digest_pool(str(tmp_path), 10)  # newest-first, gist + gistless lines
    gistless_core = "\n".join([_DIGEST_HEADER, *(e.gistless_line for e in pool)])
    gist_extra = len(pool[0].line) - len(pool[0].gistless_line)  # per-gist bytes (uniform here)

    # A budget that fits every topic line plus exactly the two newest gists.
    cap = (len(gistless_core) + 2 * gist_extra) // 4
    res = load_digest("newest", memory_dir=str(tmp_path), max_tokens=cap)

    assert [e.session_id for e in res.entries] == ["chat-0", "chat-1", "chat-2", "chat-3", "chat-4"]  # none dropped
    kept_gists = [e.session_id for e in res.entries if gist in e.line]
    assert kept_gists == ["chat-0", "chat-1"]  # oldest gists shed first, newest kept
    assert len("\n".join(res.block.splitlines()[1:-1])) // 4 <= cap

    # A budget that fits only the bare topic lines sheds every gist but keeps all 5 chats.
    bare = max(1, len(gistless_core) // 4)
    res_bare = load_digest("newest", memory_dir=str(tmp_path), max_tokens=bare)
    assert [e.session_id for e in res_bare.entries] == ["chat-0", "chat-1", "chat-2", "chat-3", "chat-4"]
    assert gist not in res_bare.block  # all gists gone, no chat lost


@pytest.mark.parametrize("provenance", [None, {"audience": "mixed", "version": "1"}, {"audience": "public", "version": "old"}])
def test_public_projection_filters_open_closed_relevant_and_cached_prompts(tmp_path, monkeypatch, provenance):
    from datetime import datetime, timezone
    from langchain_core.messages import AIMessage, HumanMessage
    from graph import public_memory
    from graph.middleware import memory
    from graph.middleware.knowledge import KnowledgeMiddleware
    from graph.session_search import search_session_summaries
    from tests._public_memory_source import PublicMemorySource

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
    _quiet_working_state(monkeypatch)
    _write(tmp_path, "chat-1", "PRIVATE-CANARY", gist="PRIVATE-GIST")
    path = tmp_path / "chat-1.json"
    old = json.loads(path.read_text())
    if provenance is not None:
        old[public_memory.PROVENANCE] = provenance
    path.write_text(json.dumps(old))
    mw = KnowledgeMiddleware(None)
    mw.compose_context({"session_id": "peer", "messages": [HumanMessage(content="hello")]}, record=False)
    search_session_summaries("PRIVATE", memory_dir=str(tmp_path))  # prime the FTS cache
    source = PublicMemorySource(tmp_path / "source.record")
    source.save([HumanMessage(content="rocket launch"), AIMessage(content="ready")])
    monkeypatch.setattr(public_memory, "_source_reader", source)
    closed = tmp_path / "closed"
    closed.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    (closed / path.name).write_text(json.dumps({**old, "closed_at": now}))
    assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now)
    assert search_session_summaries("PRIVATE", memory_dir=str(tmp_path)) == []
    assert memory.digest_entry(old)["topic"] == ""
    assert memory.format_session_summary(old) == ""
    assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now, source_card=old,
                                            audience="public", source_version=1)
    for policy in ("newest", "relevant"):
        assert load_digest(policy, query="PRIVATE", memory_dir=str(tmp_path)).entries == []
    assert "PRIVATE" not in (mw.compose_context({"session_id": "peer", "messages": []}, record=False) or {}).get("context", "")
    memory._persist_session({"session_id": "chat-1", "incognito": False,
                             "messages": [HumanMessage(content="PRIVATE-COMPACTED"), AIMessage(content="PRIVATE-REPLY")]}, "trace")
    card = json.loads(path.read_text())
    assert "PRIVATE" not in json.dumps(card)
    for policy in ("newest", "relevant"):
        assert "rocket launch" in load_digest(policy, query="rocket", memory_dir=str(tmp_path)).block
    card.update(audience="public", source_version=1)
    assert memory.record_closed_session("chat-1", topic="ignored", gist="ignored", closed_at=now, source_card=card,
                                        audience="mixed", source_version=0)
    closed_card = json.loads((closed / path.name).read_text())
    assert closed_card["audience"] == "public" and closed_card["source_version"] == 1
    path.unlink()  # closed record outlives the open card
    monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
    restarted = KnowledgeMiddleware(None)
    context = restarted.compose_context({"session_id": "peer", "messages": []}, record=False)
    assert "rocket launch" in context["context"] and "PRIVATE" not in context["context"]
    source.path.unlink()  # failed source read must also invalidate cached delivery
    memory._persist_session({"session_id": "chat-1", "incognito": False,
                             "messages": [HumanMessage(content="PRIVATE-UNKNOWN")]}, "trace")
    assert not path.exists()
    assert load_digest(memory_dir=str(tmp_path)).entries == []
    assert "rocket launch" not in restarted.compose_context({"session_id": "peer", "messages": []}, record=False)["context"]
