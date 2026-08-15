"""ADR 0071 D3 S1/S2 (#2721) — install-time trust resolution + config persistence.

Trust decides ONLY whether the console's one-time "this runs code" ack is required;
install ≠ enable ≠ trust (ADR 0027) is untouched. The ack API + dialog (S4/S6) sit on
top of these primitives.
"""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.plugins.trust import ack_pattern, normalize_source, source_official, source_trusted

OFFICIAL = ["github.com/protoLabsAI/*"]


def test_normalize_source_unifies_spellings():
    spellings = [
        "https://github.com/protoLabsAI/cowork-stack",
        "https://github.com/protoLabsAI/cowork-stack.git",
        "git@github.com:protoLabsAI/cowork-stack.git",
        "ssh://github.com/protoLabsAI/cowork-stack/",
        # scheme AND git@ together — the 2733 review's fail-safe miss: the old
        # single-alternation strip left "git@" behind and this spelling never matched
        "ssh://git@github.com/protoLabsAI/cowork-stack.git",
    ]
    assert {normalize_source(s) for s in spellings} == {"github.com/protoLabsAI/cowork-stack"}


def test_exact_ack_does_not_trust_name_collisions():
    """The 2733 review's fix-first finding: the old bare-`*` prefix widening turned an
    exact acked repo into a glob, silently trusting `<repo>-evil`. The widening is
    path-boundary only now."""
    acked = ["github.com/somebody/thing"]
    assert source_trusted("https://github.com/somebody/thing", official=[], acked=acked)
    assert source_trusted("https://github.com/somebody/thing/sub", official=[], acked=acked)
    assert not source_trusted("https://github.com/somebody/thing-evil", official=[], acked=acked)
    # …and the same boundary applies to official entries written as a bare org prefix
    assert source_official("https://github.com/org/repo", ["github.com/org"])
    assert not source_official("https://github.com/org-evil/repo", ["github.com/org"])


def test_installer_allowlist_shares_the_boundary_fix():
    """`installer._source_allowed` carried the byte-identical widening — an allowlisted
    `github.com/org` admitted `github.com/org-evil`. Patched together with trust.py
    (the 2733 review: 'patch both or the trust-side fix leaves the installer path
    vulnerable')."""
    from graph.plugins.installer import _source_allowed

    assert _source_allowed("https://github.com/org/repo", ["github.com/org"])
    assert not _source_allowed("https://github.com/org-evil/repo", ["github.com/org"])
    assert _source_allowed("ssh://git@github.com/org/repo.git", ["github.com/org/*"])
    # The 2739 re-review's major: an exact-repo entry must match the canonical .git
    # spelling — the old bare-`*` covered it by accident, so the boundary fix has to
    # cover it by NORMALIZATION (one shared normalize_source, .git trimmed).
    assert _source_allowed("https://github.com/acme/thing.git", ["github.com/acme/thing"])
    assert _source_allowed("https://github.com/acme/thing/", ["github.com/acme/thing"])
    assert not _source_allowed("https://github.com/acme/thing-evil.git", ["github.com/acme/thing"])
    # …and the REVERSE direction (round 2's major): entries hand-written in a
    # canonical spelling normalize too — a `.git`/trailing-slash/git@-spelled entry
    # keeps matching its own repo instead of silently fail-closing.
    assert _source_allowed("https://github.com/acme/thing", ["github.com/acme/thing.git"])
    assert _source_allowed("https://github.com/acme/thing", ["github.com/acme/thing/"])
    assert _source_allowed("https://github.com/acme/thing", ["git@github.com:acme/thing.git"])
    assert not _source_allowed("https://github.com/acme/thing-evil", ["github.com/acme/thing.git"])
    assert source_trusted(
        "https://github.com/acme/thing", official=[], acked=["github.com/acme/thing.git"]
    )


def test_official_org_glob_matches_every_repo_in_the_org():
    assert source_official("https://github.com/protoLabsAI/social-stack", OFFICIAL)
    assert source_official("git@github.com:protoLabsAI/anything.git", OFFICIAL)
    assert not source_official("https://github.com/evil/social-stack", OFFICIAL)


def test_trusted_via_official_ack_or_global_switch():
    url = "https://github.com/somebody/thing"
    assert not source_trusted(url, official=OFFICIAL, acked=[])
    assert source_trusted(url, official=OFFICIAL, acked=["github.com/somebody/thing"])
    assert source_trusted(url, official=OFFICIAL, acked=[], trust_unverified=True)
    # an acked entry is a glob too — the operator can hand-widen it
    assert source_trusted(url, official=[], acked=["github.com/somebody/*"])


def test_ack_pattern_is_the_exact_repo_not_the_org():
    # Narrowest grant: acking one repo trusts that repo. Org-wide trust is what
    # `official` is for (fork-overridable), not a side effect of one confirm.
    assert ack_pattern("git@github.com:somebody/thing.git") == "github.com/somebody/thing"


def test_config_defaults_and_from_dict_absent_vs_explicit_empty():
    # Absent key → the protoLabsAI default (fresh installs get org auto-trust).
    cfg = LangGraphConfig.from_dict({})
    assert cfg.plugins_sources_official == ["github.com/protoLabsAI/*"]
    assert cfg.plugins_sources_acked == [] and cfg.plugins_trust_unverified is False
    # EXPLICIT empty list → "no official sources" — a fork's hardening choice must
    # not be silently replaced by the default (#2691's absent-vs-empty lesson).
    cfg = LangGraphConfig.from_dict({"plugins": {"sources": {"official": []}}})
    assert cfg.plugins_sources_official == []
    # And a fork override replaces, not appends.
    cfg = LangGraphConfig.from_dict({"plugins": {"sources": {"official": ["github.com/acme/*"]}}})
    assert cfg.plugins_sources_official == ["github.com/acme/*"]


def test_trust_unverified_string_false_stays_off():
    """The 2733 review's fail-open finding: `bool("false")` is True, which would have
    silently DISABLED the consent gate on a quoted YAML value / JSON overlay /
    hand-edit. String forms parse via the module's _falsey (ambiguity → ask more)."""
    assert LangGraphConfig.from_dict({"plugins": {"trust_unverified": "false"}}).plugins_trust_unverified is False
    assert LangGraphConfig.from_dict({"plugins": {"trust_unverified": "0"}}).plugins_trust_unverified is False
    assert LangGraphConfig.from_dict({"plugins": {"trust_unverified": "true"}}).plugins_trust_unverified is True
    assert LangGraphConfig.from_dict({"plugins": {"trust_unverified": True}}).plugins_trust_unverified is True
    assert LangGraphConfig.from_dict({"plugins": {"trust_unverified": False}}).plugins_trust_unverified is False


def test_ack_store_survives_the_write_path():
    """The June audit's exact failure shape: an ack that doesn't persist through
    config_to_dict re-asks forever. sources.acked / official / trust_unverified must
    all ride the plugins section."""
    from graph.config_io import config_to_dict

    cfg = LangGraphConfig.from_dict(
        {
            "plugins": {
                "sources": {"official": ["github.com/acme/*"], "acked": ["github.com/x/y"]},
                "trust_unverified": True,
            }
        }
    )
    out = config_to_dict(cfg)["plugins"]
    assert out["sources"]["official"] == ["github.com/acme/*"]
    assert out["sources"]["acked"] == ["github.com/x/y"]
    assert out["trust_unverified"] is True


def test_glob_entries_keep_their_git_suffix_semantics():
    """The 2739 round-3 fail-open: trimming .git on a GLOB changed its meaning —
    `github.com/acme/*.git` became `…/acme/*` (the whole org) and a bare `*.git`
    became `*` (everything). Glob entries keep their suffix; exact entries stay
    spelling-insensitive."""
    from graph.plugins.installer import _source_allowed

    # a glob written to match only .git-suffixed paths must NOT widen
    assert not _source_allowed("https://github.com/acme/thing", ["github.com/acme/*.git"])
    assert not source_trusted("https://github.com/x/y", official=["*.git"], acked=[])
    # …while an EXACT entry's .git is spelling, trimmed as before
    assert _source_allowed("https://github.com/acme/thing", ["github.com/acme/thing.git"])
    # ordinary globs unaffected
    assert _source_allowed("https://github.com/acme/thing.git", ["github.com/acme/*"])
