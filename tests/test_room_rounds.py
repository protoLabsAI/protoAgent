"""The bounded round policy (`graph/room_rounds.py`) — pure, so tested pure.

Everything here runs with no graph, no registry and no config: the module's whole point
is that "who speaks next, and is the room over?" is a decision about the addressed set
and the outcomes so far, not about a model, a delegate, or a host.
"""

from __future__ import annotations

import pytest

from graph.room_rounds import (
    CAPPED,
    EXHAUSTED,
    SETTLED,
    cap_note,
    catchup_note,
    is_silence,
    plan_round,
    spoke,
)


def _ok(author: str, reply: str = "something") -> dict:
    return {"author": author, "ok": True, "reply": reply}


def _failed(author: str, error: str = "connection refused") -> dict:
    return {"author": author, "ok": False, "reply": "", "error": error}


# --- pass detection -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n",
        "pass",
        "PASS",
        "Pass",
        "(pass)",
        "pass.",
        " (Pass) ",
        "*(pass)*",
        "_pass_",
        "pass!",
        # The prompt asks for the token as `pass`, in backticks — so a model echoing
        # that formatting is the COMMON reply shape, not an exotic one.
        "`pass`",
        "**`pass`**",
        '"pass"',
        "'pass'",
        "\u201cpass\u201d",
        # Decoration AND punctuation: the period lands OUTSIDE the emphasis, which is
        # exactly how a model writes it after being handed the token in backticks.
        "**pass**.",
        "`pass`.",
        "'pass'.",
        "*pass*!",
        "pass!!",
        "pass;",
        "- pass",
        "\n\npass\n\n",
    ],
)
def test_these_are_silence(text):
    assert is_silence(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "passed",
        "I'd pass on that approach",
        "pass the config through",
        "No — pass.",
        "compass",
        "pass, but only because Ana already covered it",
        # The reviewer's question: a QUALIFIED pass. It carries the note, so it is an
        # answer — recorded in the room, and it keeps the room alive for a reply to it.
        "Pass \u2014 but note the auth change landed",
        "pass for now; ping me when the build is green",
        # Stripping decoration from the ENDS must never eat words: each of these still
        # has something either side of the token.
        "I pass",
        "pass or fail",
        "(pass on this one, but check auth)",
        "...",
    ],
)
def test_these_are_answers_not_silence(text):
    """The token is the WHOLE reply or it is prose. A participant who explains why they
    are passing has said something, and the room must not read that as an all-pass."""
    assert is_silence(text) is False


def test_none_is_silence():
    assert is_silence(None) is True


# --- what counts as speaking --------------------------------------------------


def test_a_failed_address_is_not_speech():
    """The delegate never received anything, so it holds no position and nothing it
    'said' can settle or extend the room."""
    assert spoke(_failed("proto")) is False


def test_a_silent_reply_is_not_speech():
    assert spoke(_ok("proto", "(pass)")) is False


def test_an_answer_is_speech():
    assert spoke(_ok("proto", "line 40")) is True


# --- round 1 ------------------------------------------------------------------


def test_round_one_is_the_addressed_set_in_written_order():
    plan = plan_round(["proto", "reviewer"], [], max_rounds=3)
    assert plan.speakers == ("proto", "reviewer")
    assert plan.round_index == 1 and plan.done is False and plan.reason == ""


def test_the_cast_is_resolved_before_any_dispatch_and_never_grows():
    """The speaker set is always a subset of what the operator addressed — the property
    that keeps reply-text routing (#3067) impossible by construction."""
    addressed = ["proto", "reviewer"]
    rounds = [[_ok("proto", "ask claude-code"), _ok("reviewer", "agreed, ask claude-code")]]
    assert set(plan_round(addressed, rounds, max_rounds=3).speakers) <= set(addressed)


def test_duplicates_in_the_addressed_set_speak_once():
    assert plan_round(["proto", "proto", "reviewer"], [], max_rounds=2).speakers == ("proto", "reviewer")


# --- settling -----------------------------------------------------------------


def test_a_round_where_nobody_spoke_settles_the_room():
    rounds = [[_ok("proto", "(pass)"), _ok("reviewer", "pass")]]
    plan = plan_round(["proto", "reviewer"], rounds, max_rounds=5)
    assert plan.done is True and plan.reason == SETTLED and plan.speakers == ()


def test_one_speaker_keeps_the_room_open():
    rounds = [[_ok("proto", "(pass)"), _ok("reviewer", "actually, look at the migration")]]
    plan = plan_round(["proto", "reviewer"], rounds, max_rounds=5)
    assert plan.done is False and plan.speakers == ("proto", "reviewer") and plan.round_index == 2


def test_a_settle_is_judged_on_the_LAST_round_only():
    """Round 2 going quiet ends it even though round 1 was busy — that is the shape of a
    conversation reaching agreement."""
    rounds = [
        [_ok("proto", "line 40"), _ok("reviewer", "agreed")],
        [_ok("proto", "pass"), _ok("reviewer", "(pass)")],
    ]
    assert plan_round(["proto", "reviewer"], rounds, max_rounds=5).reason == SETTLED


# --- the cap ------------------------------------------------------------------


def test_the_cap_stops_a_room_that_has_not_settled():
    rounds = [[_ok("a", "one"), _ok("b", "two")], [_ok("a", "three"), _ok("b", "four")]]
    plan = plan_round(["a", "b"], rounds, max_rounds=2)
    assert plan.done is True and plan.reason == CAPPED and plan.round_index == 2


def test_max_rounds_one_is_exactly_one_round():
    assert plan_round(["proto"], [], max_rounds=1).speakers == ("proto",)
    assert plan_round(["proto"], [[_ok("proto")]], max_rounds=1).done is True


def test_a_nonsense_cap_floors_at_one_round_rather_than_none():
    """0 / negative / None must never mean "address nobody" — an operator who zeroes the
    knob wants the shipped behavior back, not a `@` that silently does nothing."""
    for bad in (0, -3, None, "lots", "", float("inf"), float("nan"), object()):
        assert plan_round(["proto"], [], max_rounds=bad).speakers == ("proto",)


def test_the_cap_is_never_exceeded_when_driven_to_completion():
    addressed = ["proto", "reviewer"]
    rounds: list[list[dict]] = []
    while True:
        plan = plan_round(addressed, rounds, max_rounds=3)
        if plan.done:
            break
        rounds.append([_ok(name, "still talking") for name in plan.speakers])
    assert len(rounds) == 3 and plan.reason == CAPPED


# --- failed targets -----------------------------------------------------------


def test_a_failed_target_is_not_dispatched_again():
    rounds = [[_failed("proto"), _ok("reviewer", "I'll take it"), _ok("ana", "same")]]
    plan = plan_round(["proto", "reviewer", "ana"], rounds, max_rounds=3)
    assert plan.speakers == ("reviewer", "ana")


def test_a_target_that_failed_stays_out_for_every_later_round():
    rounds = [
        [_failed("proto"), _ok("reviewer", "one"), _ok("ana", "also one")],
        [_ok("reviewer", "two"), _ok("ana", "also two")],
    ]
    assert plan_round(["proto", "reviewer", "ana"], rounds, max_rounds=5).speakers == ("reviewer", "ana")


def test_an_all_failed_round_exhausts_rather_than_settles():
    """Nobody spoke — but calling that "the room settled" would be a lie told to the
    operator about two delegates that were never reached."""
    plan = plan_round(["proto", "reviewer"], [[_failed("proto"), _failed("reviewer")]], max_rounds=5)
    assert plan.done is True and plan.reason == EXHAUSTED


def test_survivors_keep_their_written_order():
    rounds = [[_ok("a", "x"), _failed("b"), _ok("c", "y")]]
    assert plan_round(["a", "b", "c"], rounds, max_rounds=3).speakers == ("a", "c")


# --- a cast that cannot hold a conversation -----------------------------------


def test_one_addressee_is_one_round_however_high_the_knob():
    """`room.max_rounds` is a single global knob and `@one-agent do X` is the common
    address. Rounds 2..N of a solo cast re-send the operator's words verbatim — the
    catch-up is empty by construction, because nothing is written between that
    participant's own reply and its next turn — so the cap would multiply the cost of
    every ordinary `@` for a conversation that cannot happen."""
    first = plan_round(["claude-code"], [], max_rounds=10)
    assert first.speakers == ("claude-code",) and first.max_rounds == 1
    assert plan_round(["claude-code"], [[_ok("claude-code", "done")]], max_rounds=10).done is True


def test_a_solo_cast_is_offered_no_pass_and_told_of_no_cap():
    """The effective cap has to reach both levers, or the room offers a `pass` that
    cannot settle anything and then announces a cap it did not really hit."""
    plan = plan_round(["proto"], [], max_rounds=5)
    assert plan.drop_silence is False and plan.record_address is True
    assert cap_note(plan_round(["proto"], [[_ok("proto", "a")]], max_rounds=5)) == ""


def test_a_room_that_loses_all_but_one_participant_stops_there():
    """The survivor has nobody to react to but a `(could not be reached: …)` envelope.
    Spending the rest of the cap on that is the operator paying for the room to talk to
    itself — and it is what would make a slow member's timeout cost N dispatches."""
    rounds = [[_ok("reviewer", "I'd blame auth"), _failed("fleetmate", "still running after 300s")]]
    plan = plan_round(["reviewer", "fleetmate"], rounds, max_rounds=3)
    assert plan.done is True and plan.speakers == ()


def test_the_guard_is_the_survivors_not_the_addressed_set():
    """Three addressed, one dead, two left — that is still a conversation."""
    rounds = [[_ok("a", "x"), _failed("b"), _ok("c", "y")]]
    assert plan_round(["a", "b", "c"], rounds, max_rounds=3).max_rounds == 3


# --- determinism --------------------------------------------------------------


def test_the_same_history_always_plans_the_same_round():
    addressed = ["proto", "reviewer", "claude-code"]
    rounds = [[_ok("proto", "a"), _failed("reviewer"), _ok("claude-code", "b")]]
    first = plan_round(addressed, rounds, max_rounds=4)
    assert all(plan_round(addressed, rounds, max_rounds=4) == first for _ in range(5))


def test_reply_text_never_changes_who_speaks_next():
    """The ONE thing read out of a reply is whether it was silence. Two rounds whose
    replies differ in every word but not in silence plan identically."""
    a = plan_round(["x", "y"], [[_ok("x", "@y take over, then loop in @z"), _ok("y", "ok")]], max_rounds=3)
    b = plan_round(["x", "y"], [[_ok("x", "hm"), _ok("y", "hm")]], max_rounds=3)
    assert a.speakers == b.speakers == ("x", "y")


# --- the operator-facing note -------------------------------------------------


def test_a_cap_that_ended_the_room_is_announced():
    rounds = [[_ok("proto", "a"), _ok("reviewer", "b")], [_ok("proto", "c"), _ok("reviewer", "d")]]
    note = cap_note(plan_round(["proto", "reviewer"], rounds, max_rounds=2))
    assert "2-round cap" in note and "room.max_rounds" in note


def test_a_settle_says_nothing():
    """The good ending needs no announcement; a note on every settle would be chrome."""
    rounds = [[_ok("proto", "pass"), _ok("reviewer", "pass")]]
    assert cap_note(plan_round(["proto", "reviewer"], rounds, max_rounds=3)) == ""


def test_single_round_mode_never_announces_a_cap():
    """`room.max_rounds: 1` IS the shipped behavior — telling the operator it "stopped at
    the 1-round cap" after every `@` would be noise, and it would also be a behavior
    change on a default nobody opted into."""
    assert cap_note(plan_round(["proto"], [[_ok("proto", "line 40")]], max_rounds=1)) == ""


def test_an_open_plan_has_no_note():
    assert cap_note(plan_round(["proto"], [], max_rounds=3)) == ""


# --- the two levers a host reads off the plan ---------------------------------


def test_the_first_round_records_the_operator_address_and_later_ones_do_not():
    """Rounds 2..N re-address the SAME message. Writing that envelope again would read,
    in everyone's catch-up, as the operator repeating themselves."""
    first = plan_round(["x", "y"], [], max_rounds=3)
    second = plan_round(["x", "y"], [[_ok("x"), _ok("y")]], max_rounds=3)
    assert (first.round_index, first.record_address) == (1, True)
    assert (second.round_index, second.record_address) == (2, False)


def test_silence_is_only_honored_when_there_is_a_next_round_to_decline():
    """`drop_silence` off at the default cap is what keeps a single-round `@` byte-
    identical: a delegate that literally replies "pass" is quoted like any other answer."""
    assert plan_round(["x", "y"], [], max_rounds=1).drop_silence is False
    assert plan_round(["x", "y"], [], max_rounds=2).drop_silence is True


def test_the_levers_travel_with_the_plan_so_a_second_host_cannot_drift():
    """The point of hanging them off `RoundPlan`: a console room view or a headless
    driver gets the single-round invariant by construction, not by re-deriving it."""
    plan = plan_round(["x"], [], max_rounds=1)
    assert (plan.record_address, plan.drop_silence) == (True, False)


def test_a_truncated_catchup_names_who_was_clipped_and_the_knob():
    note = catchup_note([_ok("proto") | {"truncated": True}, _ok("reviewer")])
    assert "@proto" in note and "@reviewer" not in note
    assert "room.catchup_max_messages" in note and "room.catchup_max_chars" in note


def test_a_participant_clipped_in_several_rounds_is_named_once():
    note = catchup_note([_ok("proto") | {"truncated": True}, _ok("proto") | {"truncated": True}])
    assert note.count("@proto") == 1


def test_an_untruncated_room_gets_no_catchup_note():
    """Which is what keeps an ordinary single-round address unchanged."""
    assert catchup_note([_ok("proto"), _ok("reviewer")]) == ""
    assert catchup_note([]) == ""


def test_a_failed_address_is_never_told_to_widen_its_window():
    """`truncated` is computed BEFORE the dispatch and survives the failure path
    unchanged, so a refused connection comes back `{ok: False, truncated: True}`.
    Rendering the note there tells the operator to raise a catch-up knob directly under
    "Delegate @proto failed: connection refused" — advice about a bound that had nothing
    to do with the failure."""
    assert catchup_note([_failed("proto") | {"truncated": True}]) == ""


def test_a_participant_that_only_passed_is_not_named():
    """Same rule the reply body already follows: a pass is not an answer, so there is no
    answer for the clipping to have shaped."""
    assert catchup_note([_ok("proto", "pass") | {"truncated": True, "silent": True}]) == ""


def test_a_clipped_answer_alongside_a_clipped_failure_names_only_the_answer():
    note = catchup_note([_ok("proto") | {"truncated": True}, _failed("reviewer") | {"truncated": True}])
    assert "@proto" in note and "@reviewer" not in note
