"""Copy guard for the homepage "For developers" cards (sites/marketing/src/pages/index.astro).

A CodeRabbit finding on #4770 flagged the third `devs` card for claiming local state lives
"on your machine" — wrong for headless and server installs, where the agent (and its SQLite
state) runs wherever it is deployed. The page's own FAQ already says the agent "runs where you
install it and keeps its local state there", so the card must speak the same way. These assert
the corrected phrasing survives and the old claim does not creep back in.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "sites" / "marketing" / "src" / "pages" / "index.astro"

_EXPECTED_BODY = (
    "Memory, knowledge and tasks stay in SQLite where your agent runs, "
    "so context carries from one session to the next. MIT-licensed, with no telemetry."
)


def test_developer_memory_card_says_where_your_agent_runs() -> None:
    text = INDEX.read_text(encoding="utf-8")

    assert _EXPECTED_BODY in text
    # The headless/server installs the FAQ describes don't keep state "on your machine".
    assert "stay in SQLite on your machine" not in text


def test_home_copy_is_consistent_with_the_faq() -> None:
    """The card and the FAQ answer must not disagree about where local state lives."""
    text = INDEX.read_text(encoding="utf-8")

    assert "The agent runs where you install it and keeps its local state there." in text
    assert "where your agent runs" in text
