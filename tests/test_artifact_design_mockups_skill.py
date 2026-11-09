"""The design-mockups skill (plugins/artifact/skills/design-mockups): it loads, routes, teaches the
workflow in order, and ships a scaffold that keeps its toggle bar, accessibility and render check."""

import re
from pathlib import Path

from graph.skills.loader import _MAX_DESCRIPTION, parse_skill_md, skill_md_problems

SKILL_DIR = Path(__file__).resolve().parent.parent / "plugins" / "artifact" / "skills" / "design-mockups"
SKILL_MD = SKILL_DIR / "SKILL.md"


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _scaffold() -> str:
    return re.search(r"```html\n(.*?)```", _text(), re.S)[1]


def test_skill_loads_and_its_description_is_not_truncated():
    assert skill_md_problems(_text()) == []
    skill = parse_skill_md(SKILL_MD)
    assert skill is not None and skill.name == "design-mockups"
    assert len(skill.description) <= _MAX_DESCRIPTION
    assert (SKILL_DIR / "evals.md").is_file()


def test_workflow_names_the_tools_in_order():
    body = _text().split("## The workflow", 1)[1].split("## The scaffold", 1)[0]
    order = ["show_artifact", "check_artifact", "update_artifact", "open_artifact"]
    assert [body.index(t) for t in order] == sorted(body.index(t) for t in order)
    assert 'placement="panel"' in body and "real data" in body.lower()


def test_scaffold_is_accessible_theme_aware_and_self_checking():
    html = _scaffold()
    # Theme: System removes data-theme; Light/Dark force it. Width: 1440 and 390. Every state.
    for needle in ('data-theme-btn="system"', 'data-theme-btn="light"', 'data-theme-btn="dark"',
                   'data-w-btn="desk"', 'data-w-btn="phone"', 'removeAttribute("data-theme")',
                   '["desk", 1440], ["phone", 390]', "window.__artErr"):
        assert needle in html, needle
    for state in ("empty", "one", "many", "loading", "failed"):
        assert re.search(rf"\b{state}:\s*{{ label:", html), state
    # Accessible: pressed state on every toggle, labelled groups, a labelled select, a live region.
    assert html.count('aria-pressed="false"') == 5 and html.count('role="group" aria-labelledby=') == 2
    assert '<label for="mk-state">' in html and 'role="status"' in html
    # Design system only: no literal colours in the scaffold's CSS.
    css = html.split("</style>", 1)[0]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|oklch\(", css)
    # It rides a srcdoc: a literal "</" inside its script would end the artifact's own script early.
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    assert "</script" not in script
