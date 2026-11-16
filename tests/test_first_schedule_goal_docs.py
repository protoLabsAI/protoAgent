"""The first-schedule tutorial and goal-mode guide must describe the Work flow the
console actually ships, not the retired Schedule-card / Goals-panel surfaces.

Background. The four-group Work consolidation (ADR 0129) replaced "New schedule →
Cron → Job id" with one free-text box ("What should the agent do, and when?") and a
*Check this before saving* confirm step whose Repeat choices are plain words, never
cron — ``WorkCreateDialog`` / ``workPersistence`` / ``sections/schedules.tsx``. Goals
lost their standalone panel: ``sections/goals.tsx`` registers them in **Doing now**,
and the only create entry points left are the palette's **Work: New goal…** command,
``/goal new`` in chat, and ``POST /api/goals``.

Every UI label the two docs lean on is re-derived from the shipped TypeScript (read as
text, so the test stays cheap and host-free), and the retired labels are asserted gone,
so the docs redden the moment the prose drifts back to the old surfaces. The changelog
check mirrors the other ``*_docs.py`` suites: the fragment (pre-release) or its released
top-level bullet (post-release).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TUTORIAL = REPO / "docs" / "tutorials" / "first-schedule.md"
GUIDE = REPO / "docs" / "guides" / "goal-mode.md"
FRAGMENT = REPO / "changelog.d" / "docs-first-schedule.docs.md"
CHANGELOG_MD = REPO / "CHANGELOG.md"

WEB = REPO / "apps" / "web" / "src"
CREATE_SRC = WEB / "work" / "WorkCreateDialog.tsx"
PERSIST_SRC = WEB / "work" / "workPersistence.ts"
SCHED_SRC = WEB / "work" / "sections" / "schedules.tsx"
GOALS_SRC = WEB / "work" / "sections" / "goals.tsx"
PALETTE_SRC = WEB / "app" / "palette" / "registry.ts"
SLASH_SRC = WEB / "chat" / "coreSlashCommands.ts"
ACTIVITY_SRC = WEB / "activity" / "ActivityWidget.tsx"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _flat(p: Path) -> str:
    """Doc text with runs of whitespace collapsed, so a label that line-wraps across a
    hard break (``**Check\\nthis before saving**``) still matches as one phrase — the
    assertions pin the words, not the fill column."""
    return " ".join(_text(p).split())


# --- the create flow labels the tutorial quotes are the shipped ones -----------


def test_create_dialog_ships_the_labels_the_tutorial_uses() -> None:
    """``WorkCreateDialog`` ships the step titles, the one free-text question, and the
    Edit affordances the tutorial names — so the doc can't quote a label the dialog
    dropped."""
    src = _text(CREATE_SRC)
    assert '"Check this before saving"' in src, "the confirm step title the tutorial uses"
    assert "What should the agent do, and when?" in src, "the single free-text question"
    assert '"Edit follow-up"' in src, "the schedule Edit dialog title"
    assert "Save changes" in src, "the Edit save button the tutorial names"


def test_repeat_choices_are_plain_words_not_cron() -> None:
    """The Repeat picker offers plain-words presets (``workPersistence``). The tutorial
    uses Hourly/Daily and promises no raw cron; pin the labels it leans on."""
    src = _text(PERSIST_SRC)
    for label in ('label: "Once"', 'label: "Every 30 min"', 'label: "Hourly"', 'label: "Daily"'):
        assert label in src, f"workPersistence must ship Repeat choice {label}"


def test_schedule_section_ships_coming_up_delete_and_undo() -> None:
    """Schedules register under **Coming up**; the detail's Delete… confirm and its
    undo-for-a-few-seconds copy are what the clean-up step describes."""
    src = _text(SCHED_SRC)
    assert 'group: "coming-up"' in src, "schedules live in Coming up"
    assert '"Delete…"' in src, "the Delete action label the clean-up step uses"
    assert "Delete this schedule?" in src, "the delete confirm prompt"
    assert "You can undo this for a few seconds." in src, "the undo window the doc states"


def test_feed_pill_is_named_feed() -> None:
    """Runs are read on the **Feed** pill (ActivityWidget), the label the verify step uses."""
    assert 'dialogTitle="Feed"' in _text(ACTIVITY_SRC)


def test_tutorial_walks_the_new_flow_and_drops_the_old_one() -> None:
    doc = _flat(TUTORIAL)
    # The new path: Work ▸ Coming up ▸ New → type → check → save.
    assert "Coming up" in doc
    assert "What should the agent do, and when?" in doc
    assert "Continue" in doc
    assert "Check this before saving" in doc
    for field in ("First run", "Repeat", "Then"):
        assert field in doc, f"the confirm form field {field!r} must be named"
    for choice in ("Every 30 min", "Hourly", "Daily"):
        assert choice in doc, f"the Repeat choice {choice!r} must be named"
    assert "Technical details" in doc, "the id/cron/timezone destination must be named"
    assert "Save changes" in doc and "Delete…" in doc and "Feed" in doc
    # The retired Schedule-card / cron / job-id surface must be gone.
    for stale in ("New schedule", "**Cron**", "*/2 * * * *", "Job id** to"):
        assert stale not in _text(TUTORIAL), f"retired label {stale!r} must not survive"


def test_tutorial_links_resolve() -> None:
    """The guide cross-links the tutorial keeps must point at files that exist, and the
    scheduler anchor it deep-links must be a real heading — a dead link fails the build."""
    doc = _text(TUTORIAL)
    for target in ("/guides/scheduler", "/guides/troubleshooting", "/guides/automate-work"):
        assert target in doc, f"tutorial must link {target}"
        assert (REPO / "docs" / (target.lstrip("/") + ".md")).is_file(), f"dead link: {target}"
    scheduler = _text(REPO / "docs" / "guides" / "scheduler.md")
    assert "## How firing works" in scheduler, "the #how-firing-works anchor must exist"


# --- the goal entry points the guide re-paths are the shipped ones -------------


def test_goal_create_entry_points_ship() -> None:
    """The palette command, the ``/goal new`` composer command, and goals-in-Doing-now
    are the entry points the guide now points at."""
    assert "Work: New goal…" in _text(PALETTE_SRC), "the palette create command label"
    assert "/goal new" in _text(SLASH_SRC), "the chat command that opens the New goal form"
    goals = _text(GOALS_SRC)
    assert 'group: "doing-now"' in goals, "goals register under Doing now"
    assert "EXTEND_BY = 4" in goals and "Add ${EXTEND_BY} iterations" in goals, "the Add 4 iterations action"
    assert '"Keep going"' in goals, "the re-arm action for a stopped goal"


def test_guide_repaths_goals_and_drops_the_panel() -> None:
    doc = _flat(GUIDE)
    assert "Work: New goal…" in doc, "the guide must point at the palette create command"
    assert "/goal new" in doc
    assert "Doing now" in doc and "Needs you" in doc, "goals are tracked in Work's groups"
    assert "Add 4 iterations" in doc and "Keep going" in doc
    # The retired Goals panel / Goals → New goal surface must be gone.
    for stale in ("Goals → New goal", "+ Goal", "Goals panel", "Open **Goals**"):
        assert stale not in _text(GUIDE), f"retired goal surface {stale!r} must not survive"


# --- changelog --------------------------------------------------------------------


def test_changelog_entry_is_present_and_collatable() -> None:
    """The collatable fragment (pre-release) or its released top-level bullet. The lead
    carries no PR number — ``collate`` stamps ``(#N)`` from the merge commit — but the
    load-bearing ``- **`` prefix must be present, and a ``#NNNN`` placeholder is rejected."""
    if FRAGMENT.is_file():
        body = _text(FRAGMENT)
        assert body.lstrip().startswith("- **"), "entry must start with a top-level bold bullet"
        assert "#NNNN" not in body and "#TBD" not in body, "no PR-number placeholder (collate rejects it)"
        return

    lead = "- **Rewrite the first-schedule tutorial and re-path goal mode for today's Work flow"
    sources = [CHANGELOG_MD, *sorted(REPO.glob("CHANGELOG-*.md"))]
    found = any(line.startswith(lead) for p in sources if p.is_file() for line in _text(p).splitlines())
    assert found, "the docs changelog entry is missing from the fragment and the collated changelog"
