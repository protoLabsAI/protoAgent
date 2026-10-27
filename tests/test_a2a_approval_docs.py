"""The A2A shell-approval docs (#4535 part 1) must describe the resume path the
code actually ships, not the typed resume shapes only a console can send.

The hazard this guards: over A2A the resume value is always the **text** of the
reply's text parts — ``executor.py`` joins them with ``context.get_user_input()``
and hands that string to the resume, and ``server.chat._resume_payload`` wraps it
without parsing JSON. So the boolean / data-object answers ``_approved`` also
accepts (``True``, ``{"approved": true}``, ``{"decision": "approve"}``) never
reach the parser as those types over A2A; only the accepted answer **strings**
work. An earlier revision of the reference listed the typed forms as A2A approve
values, so a caller following them resumed with a declined command. Every claim
below is re-derived from the shipped source, so the test reddens the moment the
docs drift back.

Imports stay light: ``tools.fs_tools._approved`` is a pure function (same import
the rest of the fs-tools suite uses); the executor / chat claims are read as
source text so the test stays cheap and host-free.
"""

from __future__ import annotations

import re
from pathlib import Path

from tools.fs_tools import _approved

REPO = Path(__file__).resolve().parent.parent
A2A_REF = REPO / "docs" / "reference" / "a2a-endpoints.md"
SANDBOX_GUIDE = REPO / "docs" / "guides" / "sandboxing.md"

EXECUTOR_SRC = REPO / "src" / "a2a_impl" / "executor.py"
CHAT_SRC = REPO / "src" / "server" / "chat.py"

# The answer strings _approved accepts (the frozen set in fs_tools._approved).
APPROVE_WORDS = ("approve", "approved", "yes", "y", "true", "ok")
DECLINE_WORDS = ("false", "no", "deny", "nope", "maybe", "")


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _approval_section(doc: str) -> str:
    """The reference's '#### Approval prompts (shell commands)' body, up to the next
    '### '/'#### ' heading — the region every approval claim must live in."""
    start = doc.index("#### Approval prompts (shell commands)")
    rest = doc[start + 1 :]
    m = re.search(r"\n#{3,4} ", rest)
    return rest[: m.start()] if m else rest


# --- the parser the docs describe ---------------------------------------------


def test_documented_approve_strings_are_exactly_what_the_parser_accepts() -> None:
    """Each word the reference lists is approved by ``_approved`` (case-insensitive,
    whitespace-stripped); the decline words are not. Pins the list to the code."""
    for w in APPROVE_WORDS:
        assert _approved(w) is True, f"{w!r} should approve"
        assert _approved(w.upper()) is True, f"{w!r} must be case-insensitive"
        assert _approved(f"  {w}\n") is True, f"{w!r} must survive whitespace"
    for w in DECLINE_WORDS:
        assert _approved(w) is False, f"{w!r} should decline"


def test_typed_resume_forms_are_console_only_not_a2a() -> None:
    """``_approved`` accepts bool / data-object forms — but those are the typed
    values a non-A2A client (the console) sends. Over A2A a JSON object reaches
    the parser as its literal string and declines, which is exactly why the docs
    must not list it as an A2A approve value."""
    # Typed forms the console can send.
    assert _approved(True) is True
    assert _approved({"approved": True}) is True
    assert _approved({"approved": False}) is False
    assert _approved({"decision": "approve"}) is True
    assert _approved({"answer": "yes"}) is True
    # The same object as the A2A wire would deliver it (a string) declines.
    assert _approved('{"approved": true}') is False
    assert _approved('{"decision": "approve"}') is False


# --- the A2A resume path is string-only (re-derived from source) ---------------


def test_a2a_resume_value_is_the_user_input_text() -> None:
    """The executor resumes with the joined text parts, and the chat resume helper
    passes that value through without JSON parsing — so over A2A the parser only
    ever sees a string. This is the fact the reference's wording hangs on."""
    executor = _text(EXECUTOR_SRC)
    assert "context.get_user_input()" in executor
    assert "self._stream_factory(" in executor
    assert "resume=resume" in executor

    chat = _text(CHAT_SRC)
    body = chat[chat.index("async def _resume_payload") :]
    body = body[: body.index("\nasync def ", 1)]
    assert "return {iid: resume_value} if iid else resume_value" in body
    assert "json.loads" not in body, "_resume_payload must not decode the A2A text back to JSON"


# --- the reference doc says it correctly ---------------------------------------


def test_reference_documents_the_string_only_a2a_answer_path() -> None:
    section = _approval_section(_text(A2A_REF))
    # It ties the A2A answer to text parts and names the code path.
    assert "always the **text**" in section
    assert "get_user_input()" in section
    assert "does not parse JSON" in section
    # Every accepted answer word appears.
    for w in APPROVE_WORDS:
        assert f"`{w}`" in section, f"approval section must list {w!r}"
    # The decline behaviour is documented.
    assert "`false`" in section and "`no`" in section and "`deny`" in section
    assert "not run" in section


def test_reference_flags_typed_forms_as_not_working_over_a2a() -> None:
    """The data-object / boolean shapes may be mentioned, but only with the
    explicit caveat that they do not work over A2A — never as A2A approve values."""
    section = _approval_section(_text(A2A_REF))
    assert '`{"approved": true}`' in section  # mentioned...
    assert "do not work over A2A" in section  # ...only to warn against it
    # The old, wrong claim must be gone.
    assert "This boolean takes precedence over other fields." not in _text(A2A_REF)


def test_reference_documents_bypass_flag_switch_and_always_ask_exceptions() -> None:
    section = _approval_section(_text(A2A_REF))
    assert "`message.metadata.bypass_permissions: true`" in section
    assert '"bypass_permissions": true' in section  # the JSON example
    assert "every shell command for that turn" in section
    assert "filesystem.bypass_allowed" in section
    assert "`delete_file` and onboarding" in section and "always ask" in section
    # Nothing promises per-caller scoping.
    assert "host-wide, not" in section and "per-caller" in section
    assert "ADR 0124" in section


def test_bypass_claims_match_the_code() -> None:
    """The flag names and the always-ask floor the reference cites are real."""
    fs = _text(REPO / "src" / "tools" / "fs_tools.py")
    assert "bypass_permissions" in fs
    assert 'getattr(config, "filesystem_bypass_allowed", True)' in fs  # default-on switch
    assert "bypass-permissions" in fs and "log.warning" in fs  # WARNING audit line
    # delete_file's approval is a floor bypass can't skip.
    assert "even bypass-permissions mode can't skip" in fs


# --- the sandboxing guide ------------------------------------------------------


def test_sandboxing_has_delegated_callers_section_recommending_allowlist() -> None:
    guide = _text(SANDBOX_GUIDE)
    assert "## Delegated and voice callers" in guide
    start = guide.index("## Delegated and voice callers")
    section = guide[start : guide.index("\n## ", start + 1)]
    # Recommends run_auto_approve over bypass, and warns bypass is turn-wide.
    assert "run_auto_approve" in section
    assert section.index("run_auto_approve") < section.index("bypass_permissions"), (
        "the allowlist must be recommended before (above) the bypass option"
    )
    assert "every shell command for that turn" in section
    # Links to the a2a reference approval section.
    assert "/reference/a2a-endpoints#approval-prompts-shell-commands" in section
    assert "bypass_allowed: false" in section
    assert "ADR 0124" in section


def test_sandboxing_healthz_note_is_durable_not_a_dev_checkout_note() -> None:
    """Round-1 minor fix: the /healthz note must not pin claims to this checkout or
    an unmerged PR's field list."""
    guide = _text(SANDBOX_GUIDE)
    assert "`GET /healthz`" in guide
    assert "this checkout's" not in guide
    assert "setup, UI tier, and model information" not in guide


def test_cross_links_resolve_to_real_pages() -> None:
    """Relative cross-links between the two docs point at files that exist — a dead
    link fails the docs build."""
    assert SANDBOX_GUIDE.is_file()
    assert A2A_REF.is_file()
    # /guides/sandboxing and /reference/a2a-endpoints are docs-root-absolute links.
    assert "/guides/sandboxing#delegated-and-voice-callers" in _text(A2A_REF)
    assert "/reference/a2a-endpoints#approval-prompts-shell-commands" in _text(SANDBOX_GUIDE)
