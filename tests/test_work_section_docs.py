"""The console guide's Work section must name what ADR 0129/0131 actually shipped.

`docs/guides/react-tauri-ui.md` is the only page that explains the console, and its
Work coverage pre-dated the ADR 0129 consolidation. This gives the Work surface the
`test_stay_awake_docs.py` treatment: every behavioural claim the new section makes is
re-derived from the shipped source — the four group labels in `apps/web/src/work/tone.ts`,
the renamed rail chip in `ChatStatusRail.tsx`, the "Started in" field and idle/stalled
labels in `work/sections/tasks.tsx`, and the stall threshold / nudge cap / park-to-waiting
behaviour in `tasks/stall.py` + `server/maintenance_loops.py`. The doc reddens the moment
the prose drifts from the code.

The changelog check requires the collatable `changelog.d/docs-work-section.docs.md`
fragment (pre-release) or its released top-level bullet in the collated changelog
(post-release, across CHANGELOG.md and the CHANGELOG-YYYY-MM.md archives).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "react-tauri-ui.md"
FRAGMENT = REPO / "changelog.d" / "docs-work-section.docs.md"
CHANGELOG_MD = REPO / "CHANGELOG.md"

TONE_SRC = REPO / "apps" / "web" / "src" / "work" / "tone.ts"
RAIL_SRC = REPO / "apps" / "web" / "src" / "chat" / "ChatStatusRail.tsx"
TASKS_SRC = REPO / "apps" / "web" / "src" / "work" / "sections" / "tasks.tsx"
STALL_SRC = REPO / "src" / "tasks" / "stall.py"
LOOPS_SRC = REPO / "src" / "server" / "maintenance_loops.py"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _flat(p: Path) -> str:
    """The guide with every run of whitespace collapsed to one space, so a prose phrase
    assertion is not defeated by the file's hand-wrapping at ~76 columns. Markdown tokens
    (`**Needs you**`, `` `stalled` ``, `](#work)`) carry no internal whitespace, so they
    survive flattening unchanged."""
    return " ".join(_text(p).split())


def test_guide_has_work_section_with_the_four_groups_and_overview() -> None:
    """The guide grows a `## Work` section naming all four groups, Overview, and the
    This-chat filter. The group labels are re-derived from the shipped `WORK_GROUP_LABELS`
    so the doc can't name a group the console doesn't."""
    guide = _flat(GUIDE)
    tone = _text(TONE_SRC)
    assert "## Work" in guide, "the guide must gain a Work section"
    for label in ("Needs you", "Doing now", "Coming up", "Your instructions"):
        assert f'"{label}"' in tone, f"WORK_GROUP_LABELS must still ship {label!r}"
        assert f"**{label}**" in guide, f"the Work section must name the {label!r} group"
    assert "**Overview**" in guide, "the Work section must mention Overview"
    assert "**This chat**" in guide, "the Work section must describe the This-chat filter"


def test_guide_describes_the_chat_status_rail_and_its_chips() -> None:
    """The Chat section replaces "background work appears above the composer" with the
    status rail: a Needs you marker and a chip per group. The renamed "Instructions" chip
    is re-derived from the rail's own label branch (`Told it` → `Instructions`, #5327)."""
    guide = _flat(GUIDE)
    rail = _text(RAIL_SRC)
    assert "background work appears above the composer" not in guide, "stale rail claim must be gone"
    assert "**Status rail:**" in guide
    assert '"Instructions"' in rail, "the rail still renders the renamed Instructions chip"
    assert "Told it" not in rail, "the Told it label was renamed in #5327"
    for chip in ("**Doing now**", "**Coming up**", "**Instructions**"):
        assert chip in guide, f"the status rail must list the {chip} chip"


def test_guide_covers_answering_a_waiting_job_in_place() -> None:
    """ADR 0129 D8: a waiting job is answered in place with Reply / Done… / Dismiss…, and
    Reply opens a "Re: <title>" chat when the source has no turns. The actions and the
    Re: title are re-derived from the waiting-detail code."""
    guide = _flat(GUIDE)
    tasks = _text(TASKS_SRC)
    assert "**Reply**" in guide and "**Done…**" in guide and "**Dismiss…**" in guide
    assert 'title: `Re: ${task.title}`' in tasks, "Reply still titles a fresh chat Re: <title>"
    assert "`Re: <the job's title>`" in guide, "the guide must show the Re: chat title"


def test_guide_covers_started_in_and_job_binding() -> None:
    """ADR 0131 D2: every open job shows "Started in", binding it to the conversation that
    created it; a background-born job reads "Started by a background job". Both are
    re-derived from the shared Started-in builder."""
    guide = _flat(GUIDE)
    tasks = _text(TASKS_SRC)
    assert '"Started in"' in tasks, "taskFields still labels the field Started in"
    assert "Started by a background job" in tasks, "the background-job provenance string"
    assert "**Started in**" in guide
    assert "Started by a background job" in guide
    # The instance line now also binds a job to its conversation.
    assert "remembers the conversation that created it" in guide


def test_guide_explains_idle_and_stalled_labels() -> None:
    """ADR 0129 D9 labels: "in progress · idle Nm" for an undriven in-progress job and
    "stalled" once it has sat untouched too long. Re-derived from `activityLead`."""
    guide = _flat(GUIDE)
    tasks = _text(TASKS_SRC)
    assert 'in progress · ${span}' in tasks, "activityLead still builds the idle lead"
    assert 'idle ${minutes}m' in tasks
    assert 'task.activity === "stalled"' in tasks
    assert "`in progress · idle 5m`" in guide, "the guide must show the idle label"
    assert "`stalled`" in guide, "the guide must show the stalled label"


def test_guide_explains_stall_reminders_and_parking() -> None:
    """The stall reminders: an undriven in-progress job past the ~30-min threshold is
    reminded up to 3 times, then parked waiting (→ Needs you). The threshold, the cap and
    the park-to-waiting are re-derived from the shipped stall sweep so the prose can't claim
    a cadence the code doesn't run."""
    guide = _flat(GUIDE)
    stall = _text(STALL_SRC)
    loops = _text(LOOPS_SRC)
    assert "STALL_THRESHOLD_S = 1800" in stall, "30-minute idle threshold the doc restates"
    assert "STALL_NUDGE_CAP = 3" in stall, "three-reminder cap the doc restates"
    assert 'status="waiting"' in loops, "the cap parks the task waiting"
    assert "Stalled: no progress after 3 reminders" in loops
    assert "up to three times" in guide, "the guide must state the three-reminder cap"
    assert "half an hour" in guide, "the guide must state the ~30-minute threshold"
    # Parked work lands back in Needs you for the operator.
    assert "moves to **Needs you**" in guide


def test_guide_right_rail_no_longer_lists_the_old_work_kinds() -> None:
    """The Layout right-rail bullet dropped the pre-0129 "goals, watches, tasks, and
    schedules" wording and points at the Work groups instead."""
    guide = _flat(GUIDE)
    assert "open Work for goals, watches, tasks, and schedules" not in guide
    assert "open [Work](#work)" in guide


def test_guide_inpage_anchors_resolve() -> None:
    """Every `#work` / `#chat` link the Work changes add has its defining heading in the
    same file — the self-contained half of the docs-build dead-link check."""
    guide = _flat(GUIDE)
    assert "## Work" in guide, "the #work anchor's heading must exist"
    assert "## Chat" in guide, "the #chat anchor's heading must exist"
    assert guide.count("](#work)") >= 1
    assert guide.count("](#chat)") >= 1


def test_changelog_entry_is_present_and_collatable() -> None:
    """Require the collatable fragment or its released top-level bullet. The fragment keeps
    its load-bearing `- **` prefix (without the leading `- ` the collator silently drops the
    entry); per this card the bold lead carries no PR number. Once collated the bullet lives
    in CHANGELOG.md, but the month's first release archives older sections into
    CHANGELOG-YYYY-MM.md, so the post-collation search spans both."""
    # No trailing period: `collate` stamps `(#N)` into the lead before the period, so this
    # prefix matches both the pre-release fragment and the stamped released bullet.
    lead = "- **Console guide covers the Work surface"
    if FRAGMENT.is_file():
        body = _text(FRAGMENT)
        assert body.lstrip().startswith("- **"), "entry must start with a top-level bullet"
        assert body.lstrip().startswith(lead), "the fragment's lead-in must match the released bullet"
        return
    sources = [CHANGELOG_MD, *sorted(REPO.glob("CHANGELOG-*.md"))]
    found = any(line.startswith(lead) for p in sources if p.is_file() for line in _text(p).splitlines())
    assert found, (
        f"the Work-section docs changelog entry is missing from {FRAGMENT.relative_to(REPO)} "
        f"(fragment, pre-release), {CHANGELOG_MD.relative_to(REPO)} (collated, post-release), and "
        f"every CHANGELOG-*.md archive: expected a top-level bullet beginning {lead!r}"
    )
