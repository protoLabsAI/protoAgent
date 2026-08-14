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
    ]
    assert {normalize_source(s) for s in spellings} == {"github.com/protoLabsAI/cowork-stack"}


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
