"""The "Stay awake while serving" docs must name what the code actually shipped (#4541).

The docs card (phone-access guide subsection + Troubleshooting row, configuration reference)
hand-writes a config key, its default, an environment variable and a platform matrix that are
*defined elsewhere in this repo* — the `runtime.stay_awake` FIELD in settings_schema, its
dataclass default in config.py, and the serving hold in infra/keep_awake.py. When one of those
moves and the prose doesn't, the doc lies under a green gate. This is the
`test_model_concurrency_docs.py` treatment for the keep-awake pages: every claim below is
re-derived from the shipped source (read as text so the test stays cheap and self-contained,
no server import), so the test reddens the moment the docs drift from the code.

The changelog check reads from EITHER source — the `changelog.d/4541-stay-awake.docs.md`
fragment (pre-release) or, once `scripts/changelog.py collate` has folded the fragment into
CHANGELOG.md and deleted it (on a release PR and on main afterwards), the collated bullet in
CHANGELOG.md — so it survives the release roll instead of hard-requiring the fragment.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "phone-access.md"
CONFIG_REF = REPO / "docs" / "reference" / "configuration.md"
FRAGMENT = REPO / "changelog.d" / "4541-stay-awake.docs.md"
CHANGELOG_MD = REPO / "CHANGELOG.md"

KEEP_AWAKE_SRC = REPO / "src" / "infra" / "keep_awake.py"
SCHEMA_SRC = REPO / "src" / "graph" / "settings_schema.py"
CONFIG_SRC = REPO / "src" / "graph" / "config.py"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_schema_and_config_ship_the_documented_key_and_default() -> None:
    """The docs advertise `runtime.stay_awake` beside `runtime.autostart_on_boot`, both
    defaulting false. Those keys and defaults are re-derived from the shipped source, so a
    doc can't advertise a setting the schema doesn't render or a default it doesn't ship."""
    schema = _text(SCHEMA_SRC)
    config = _text(CONFIG_SRC)
    assert '"runtime.stay_awake"' in schema, "settings_schema must ship runtime.stay_awake"
    assert '"runtime.autostart_on_boot"' in schema, "settings_schema must ship runtime.autostart_on_boot"
    assert "Stay awake while serving" in schema, "the shipped label the guide quotes"
    assert "stay_awake: bool = False" in config, "default must be false, as documented"
    assert "autostart_on_boot: bool = False" in config, "default must be false, as documented"


def test_guide_has_keep_awake_subsection_with_anchor() -> None:
    """phone-access.md grows a `{#keep-awake}` subsection covering the awake-only behaviour,
    the Settings toggle + its key, the restart, the AC-power / closed-lid limits, the macOS
    no-code alternative, the kill-switch env var, and no Windows support."""
    guide = _text(GUIDE)
    assert "### Keep the computer awake {#keep-awake}" in guide
    assert "the agent only answers while the computer is awake" in guide
    # Names the setting, its path and its key.
    assert "Settings ▸ Runtime" in guide
    assert "Stay awake while serving" in guide
    assert "`runtime.stay_awake`" in guide
    assert "restart" in guide
    # The platform limits.
    assert "applies only on AC power" in guide
    assert "lid" in guide
    # The macOS system-setting alternative.
    assert "Prevent automatic sleeping when the display is off" in guide
    # The kill switch and the Windows caveat.
    assert "PROTOAGENT_KEEP_AWAKE=0" in guide
    assert "Not supported on Windows" in guide


def test_guide_troubleshooting_row_links_to_keep_awake() -> None:
    """The Troubleshooting table gains an idle/slept row that links to the new anchor."""
    guide = _text(GUIDE)
    assert "after the computer has been idle" in guide
    assert "](#keep-awake)" in guide, "the idle/slept row must link to #keep-awake"


def test_guide_keep_awake_links_resolve_in_file() -> None:
    """Every `#keep-awake` link in the guide has its defining heading in the same file —
    a dead in-page anchor is the self-contained half of the docs-build link check."""
    guide = _text(GUIDE)
    assert "{#keep-awake}" in guide, "the anchor must be defined"
    assert guide.count("](#keep-awake)") >= 1


def test_config_reference_documents_runtime_stay_awake() -> None:
    """configuration.md gains a `runtime` section documenting stay_awake (default false)
    beside autostart_on_boot, with the cross-links that would otherwise fail the build."""
    cfg = _text(CONFIG_REF)
    assert "## `runtime`" in cfg
    assert "| `stay_awake` | `false` |" in cfg
    assert "| `autostart_on_boot` | `false` |" in cfg
    assert "PROTOAGENT_KEEP_AWAKE=0" in cfg
    # The two cross-links the section adds must point at files that exist.
    for target in ("../adr/0047-layered-settings-cascade.md", "../guides/phone-access.md#keep-awake"):
        assert target in cfg, f"configuration.md must link {target}"
        file_part = target.split("#", 1)[0]
        assert (CONFIG_REF.parent / file_part).resolve().is_file(), f"dead link: {target}"


def test_doc_platform_claims_match_keep_awake_module() -> None:
    """The guide's and reference's platform claims (macOS `caffeinate -s`, AC-power-only, the
    `PROTOAGENT_KEEP_AWAKE=0` kill switch) are the serving hold's own behaviour, re-derived
    from infra/keep_awake.py so a rename there can't leave the prose behind."""
    src = _text(KEEP_AWAKE_SRC)
    assert 'os.environ.get("PROTOAGENT_KEEP_AWAKE") == "0"' in src, "the env kill switch the docs cite"
    assert '["caffeinate", "-s", "-w"' in src, "the macOS serving command"
    assert "only on AC power" in src, "the AC-power limit the docs restate"


def test_changelog_entry_is_present_and_collatable() -> None:
    """The docs-kind changelog entry survives the release roll: pre-release it lives in the
    `changelog.d/4541-stay-awake.docs.md` fragment; `scripts/changelog.py collate` then folds
    it into CHANGELOG.md and DELETES the fragment. So the entry must be present in EITHER
    source, and this test accepts whichever one carries it (it can't hard-require the fragment,
    which no longer exists on a release PR or on main after the release).

    Fragment present: keep today's assertions (the load-bearing top-level `- **` bullet —
    without the leading `- ` the collator silently drops it — plus `(#4541)`). Fragment gone:
    assert CHANGELOG.md carries the collated lead-in bullet as it appears in the v0.219.0
    section. Neither source carrying the entry fails with a message naming both locations."""
    if FRAGMENT.is_file():
        body = _text(FRAGMENT)
        assert body.lstrip().startswith("- **"), "entry must start with a top-level bullet"
        assert "(#4541)" in body
        return

    collated = _text(CHANGELOG_MD)
    lead = '- **Document "Stay awake while serving" for phone access (#4541).**'
    assert any(line.startswith(lead) for line in collated.splitlines()), (
        "the #4541 docs changelog entry is missing from BOTH "
        f"{FRAGMENT.relative_to(REPO)} (fragment, pre-release) and "
        f"{CHANGELOG_MD.relative_to(REPO)} (collated, post-release): expected a top-level "
        f"bullet beginning {lead!r} in CHANGELOG.md once the fragment is collated"
    )
