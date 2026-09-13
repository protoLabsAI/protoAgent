"""deck.hitl + the conversation screen's S3 half (#3470): answering a parked turn (question /
form wizard / approval / plugin form / dismiss), steering a running turn and reconciling at
its end, interjecting into an attended server-fired turn, attaching to a turn in flight,
cancelling one delegation, and attendance following the open session."""

from __future__ import annotations

import json

import pytest
from textual.widgets import Button, Input, Markdown, Select, Static, TextArea, Tree

from deck import a2a
from deck import hitl as deckhitl
from deck.app import FleetDeck
from deck.feed import Activity
from deck.hitl import ApprovalModal, FormModal, QuestionModal
from deck.talk import ConversationScreen
from tests.test_deck_app import _settle
from tests.test_deck_feed import FakeEvents, ev
from tests.test_deck_talk import FakeA2A, TalkBackend, _open_talk, _send, _until, canned_frames, status

# ── the form rules (apps/web/src/chat/hitl-form.ts) ───────────────────────────


STEP = {
    "title": "Deploy",
    "schema": {
        "type": "object",
        "properties": {
            "env": {"type": "string", "oneOf": [{"const": "prod", "title": "Production", "description": "live"}, {"const": "stage", "title": "Staging"}]},
            "regions": {"type": "array", "items": {"enum": ["eu", "us"]}},
            "dry": {"type": "boolean", "default": False},
            "count": {"type": "integer", "default": 2},
            "why": {"type": "string", "format": "textarea", "showWhen": {"field": "env", "equals": "prod"}},
            "note": {"type": "string", "showWhen": {"field": "regions", "in": [["eu"]]}},
            "tag": {"type": "string", "showWhen": {"field": "dry"}},
        },
        "required": ["env", "why", "count"],
    },
}


def test_form_rules_mirror_the_console():
    fields = deckhitl.fields_of(STEP)
    assert [(k, r) for k, _, r in fields] == [("env", True), ("regions", False), ("dry", False), ("count", True), ("why", True), ("note", False), ("tag", False)]
    env = STEP["schema"]["properties"]["env"]
    assert deckhitl.options_of(env) == [("prod", "Production", "live"), ("stage", "Staging", "")]
    assert deckhitl.options_of(STEP["schema"]["properties"]["regions"]) == [("eu", "eu", ""), ("us", "us", "")]
    assert deckhitl.options_of({"type": "string"}) == []
    assert deckhitl.is_multi(STEP["schema"]["properties"]["regions"]) and not deckhitl.is_multi(env)
    # a default IS an answer; false and 0 are kept
    assert deckhitl.seed_defaults([STEP]) == {"dry": False, "count": 2}
    # visibility: equals / in / truthy
    assert not deckhitl.is_visible(STEP["schema"]["properties"]["why"], {})
    assert deckhitl.is_visible(STEP["schema"]["properties"]["why"], {"env": "prod"})
    assert deckhitl.is_visible(STEP["schema"]["properties"]["note"], {"regions": ["eu"]})
    assert not deckhitl.is_visible(STEP["schema"]["properties"]["note"], {"regions": ["us"]})
    assert deckhitl.is_visible(STEP["schema"]["properties"]["tag"], {"dry": True}) and not deckhitl.is_visible(STEP["schema"]["properties"]["tag"], {"dry": False})
    # required gating skips hidden fields; a boolean is always answered; a multi needs one
    assert deckhitl.missing_in_step(STEP, {"count": 2}) == ["env"]
    assert deckhitl.missing_in_step(STEP, {"env": "prod", "count": 2}) == ["why"]
    assert deckhitl.missing_in_step(STEP, {"env": "stage", "count": 2}) == []
    assert deckhitl.any_step_missing([STEP, {"schema": {"properties": {"x": {"type": "string"}}, "required": ["x"]}}], {"env": "stage", "count": 2})
    assert deckhitl.has_value({"type": "array"}, []) is False and deckhitl.has_value({"type": "boolean"}, False) is True
    # coercion from widget text
    assert deckhitl.coerce({"type": "integer"}, " 3 ") == 3 and deckhitl.coerce({"type": "number"}, "1.5") == 1.5
    assert deckhitl.coerce({"type": "integer"}, "x") is None and deckhitl.coerce({"type": "integer"}, "") is None
    assert deckhitl.coerce({"type": "array"}, ("a",)) == ["a"] and deckhitl.coerce({"type": "boolean"}, 1) is True
    assert deckhitl.coerce({"type": "string"}, 5) == "5"
    # kinds and the wire text
    assert deckhitl.kind_of({"question": "hm?"}) == "question" and deckhitl.kind_of(None) == "question"
    assert deckhitl.kind_of({"kind": "approval", "title": "Approve?"}) == "approval"
    assert deckhitl.kind_of({"kind": "form", "steps": [STEP]}) == "form" and deckhitl.kind_of({"kind": "form", "steps": []}) == "question"
    assert deckhitl.kind_of({"plugin_callback_id": "cb", "steps": [STEP]}) == "form"
    assert deckhitl.kind_of({"plugin_callback_id": "cb", "steps": []}) == "form"  # no fields: still a form to redeem ({}), never a question
    assert deckhitl.prompt_of({"kind": "approval", "title": "Approve?"}) == "Approve?" and deckhitl.prompt_of({}) == "input required"
    assert json.loads(deckhitl.answer_text("form", {"a": 1})) == {"a": 1} and deckhitl.answer_text("question", "yes") == "yes"


# ── fakes ──


def park_frame(cid: str, hitl: dict, text: str = "Input required.") -> dict:
    return status("TASK_STATE_INPUT_REQUIRED", parts=[{"text": text}, {"data": hitl, "metadata": {"mimeType": a2a.HITL_MIME}}], cid=cid)


class Parking(FakeA2A):
    """First stream parks on `hitl`; the resume (metadata.hitl_resume) completes the turn."""

    def __init__(self, hitl: dict, **kw):
        super().__init__(**kw)
        self.hitl = hitl

    def stream(self, text, *, context_id, task_id=None, metadata=None):
        self.sent.append({"text": text, "context_id": context_id, "task_id": task_id, "metadata": metadata})
        if metadata and metadata.get("hitl_resume"):
            yield from canned_frames(context_id)
            return
        yield {"result": {"task": {"id": "t1", "contextId": context_id, "status": {"state": "TASK_STATE_SUBMITTED"}}}}
        yield park_frame(context_id, self.hitl)


async def _type(app, pilot, text, wait=0.3):
    """Type and send WITHOUT settling — for messages that land while a stream hangs."""
    comp = app.screen.query_one("#composer", Input)
    comp.focus()
    comp.value = text
    await pilot.press("enter")
    await pilot.pause(wait)


def _resume_call(fake: FakeA2A, *, hidden: bool = False) -> dict:
    """The resume goes to the parked task with the console's metadata: `hitl_resume`, plus
    `hidden` for a silent (approval / dismiss) resume."""
    assert len(fake.sent) == 2, fake.sent
    call = fake.sent[1]
    assert call["task_id"] == "t1" and call["metadata"] == ({"hitl_resume": True, "hidden": True} if hidden else {"hitl_resume": True})
    return call


# ── answering ──


@pytest.mark.asyncio
async def test_approval_modal_resumes_the_parked_task_silently():
    fake = Parking({"kind": "approval", "title": "Approve shell command?", "detail": "rm -rf build\n\nruns via: /bin/zsh", "project": "web"})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "clean the build")
        st = str(app.screen.query_one("#talk-status", Static).content)
        assert "needs you (approval): Approve shell command?" in st
        assert app.activity.turn_cell("protoEngineer-ba4c") == "⚑ needs you"
        # enter on the empty composer opens the modal (not the composer text — it is empty)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ApprovalModal)
        assert "rm -rf build" in app.screen.query(".hitl-detail Static").first().render().plain
        await pilot.press("a")
        await _settle(app, pilot)
        assert isinstance(app.screen, ConversationScreen)
        assert _resume_call(fake, hidden=True)["text"] == "approved"
        assert await _until(pilot, lambda: app.screen.convo.live is None)
        # silent: the same exchange continued, no answer line, one markdown body
        assert len(app.screen.query(Markdown)) == 1 and not app.screen.query(".answer-msg")
        assert "Three PRs are open." in str(app.screen.query(Markdown).first().source)
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)
        assert app.activity.turn_cell("protoEngineer-ba4c") == "idle"


@pytest.mark.asyncio
async def test_deny_and_escape_in_the_approval_modal():
    fake = Parking({"kind": "approval", "title": "Approve permanent file delete?", "detail": "/tmp/x"})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "delete it")
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ApprovalModal)
        await pilot.press("escape")  # leaves it parked
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConversationScreen) and app.screen.convo.parked is not None and len(fake.sent) == 1
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        await pilot.press("d")
        await _settle(app, pilot)
        assert _resume_call(fake, hidden=True)["text"] == "denied"


@pytest.mark.asyncio
async def test_a_question_takes_the_typed_answer_and_shows_it():
    fake = Parking({"question": "Merge this PR?"})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "ship it")
        assert app.screen.convo.parked is not None
        await _send(app, pilot, "yes, squash it")
        assert _resume_call(fake)["text"] == "yes, squash it"
        assert await _until(pilot, lambda: app.screen.convo.live is None)
        answers = app.screen.query(".answer-msg")
        assert len(answers) == 1 and "you ›  yes, squash it" in answers.first().render().plain
        assert len(app.screen.query(Markdown)) == 1  # the exchange continued
        assert app.screen.convo.parked is None


@pytest.mark.asyncio
async def test_question_modal_carries_the_draft_and_ctrl_d_dismisses_with_the_sentinel():
    fake = Parking({"question": "Which branch?"})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "release")
        comp = app.screen.query_one("#composer", Input)
        comp.value = "mai"
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, QuestionModal)
        assert app.screen.query_one("#answer", Input).value == "mai"
        await pilot.press("ctrl+d")
        await _settle(app, pilot)
        assert _resume_call(fake, hidden=True)["text"] == a2a.DISMISS_SENTINEL
        assert not app.screen.query(".answer-msg")  # a dismissal is not conversation


@pytest.mark.asyncio
async def test_form_wizard_gates_required_fields_reveals_conditional_ones_and_submits_json():
    steps = [
        {"title": "who", "schema": {"properties": {"name": {"type": "string", "title": "Name"}, "count": {"type": "integer", "default": 2}}, "required": ["name"]}},
        {"title": "how", "schema": {"properties": {"mode": {"enum": ["fast", "safe"]}, "notes": {"type": "string", "format": "textarea", "showWhen": {"field": "mode", "equals": "safe"}}, "confirm": {"type": "boolean"}}, "required": ["mode"]}},
    ]
    fake = Parking({"kind": "form", "title": "Release", "description": "fill me", "steps": steps})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "release please")
        assert "needs you (form): Release" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        assert isinstance(app.screen, FormModal)
        modal = app.screen
        assert "step 1 / 2" in str(modal.query_one("#form-step-head", Static).content)
        assert modal.query_one("#next").disabled  # name is required and empty
        assert modal.query_one("#in-count", Input).value == "2"  # the default is an answer
        await pilot.press("ctrl+right")  # blocked while required is missing
        await pilot.pause(0.1)
        assert modal.current == 0
        modal.query_one("#in-name", Input).focus()
        await pilot.press(*"bob")
        await pilot.pause(0.1)
        assert not modal.query_one("#next").disabled
        await pilot.press("enter")  # enter in a field = next step
        await pilot.pause(0.3)
        assert modal.current == 1 and "step 2 / 2" in str(modal.query_one("#form-step-head", Static).content)
        assert not modal.query(".hitl-field#field-notes")  # hidden until mode == safe
        modal.query_one("#in-mode", Select).value = "safe"
        await pilot.pause(0.3)
        assert modal.query("#field-notes")
        modal.query_one("#in-notes", TextArea).focus()
        await pilot.press(*"go slow")
        await pilot.pause(0.1)
        await pilot.press("ctrl+left")
        await pilot.pause(0.2)
        assert modal.current == 0 and modal.query_one("#in-name", Input).value == "bob"  # answers survive Back
        await pilot.press("ctrl+right")
        await pilot.pause(0.2)
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert isinstance(app.screen, ConversationScreen)
        sent = json.loads(_resume_call(fake)["text"])
        assert sent == {"name": "bob", "count": 2, "mode": "safe", "notes": "go slow"}
        assert "you ›" in app.screen.query(".answer-msg").first().render().plain


@pytest.mark.asyncio
async def test_a_plugin_form_is_redeemed_on_the_submit_route_and_a_returned_form_is_the_next_step():
    fake = Parking({"kind": "form", "title": "Post?", "plugin_callback_id": "cb1", "steps": [{"schema": {"properties": {"text": {"type": "string"}}, "required": ["text"]}}]})
    be = TalkBackend(a2a_client=fake)
    be.form_result = {"form": {"kind": "form", "title": "Where?", "steps": [{"schema": {"properties": {"channel": {"enum": ["x", "y"]}}}}]}, "callback_id": "cb2"}
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "post the update")
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        assert isinstance(app.screen, FormModal)
        app.screen.query_one("#in-text", Input).focus()
        await pilot.press(*"hello", "ctrl+s")
        assert await _until(pilot, lambda: any(c[0] == "submit_form" for c in be.calls))
        assert any(c[0] == "submit_form" and c[2] == "cb1" and c[3] == {"text": "hello"} for c in be.calls)
        assert len(fake.sent) == 1  # never resumed the graph
        # the wizard's next step opened by itself, carrying the new callback id
        assert await _until(pilot, lambda: isinstance(app.screen, FormModal) and app.screen.hitl.get("plugin_callback_id") == "cb2")
        be.form_result = {"reply": "posted"}
        app.screen.query_one("#in-channel", Select).value = "y"
        await pilot.pause(0.2)
        await pilot.press("ctrl+s")
        assert await _until(pilot, lambda: any(c[0] == "submit_form" and c[2] == "cb2" for c in be.calls))
        await _settle(app, pilot)
        assert isinstance(app.screen, ConversationScreen) and app.screen.convo.parked is None
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)


# ── steering ──


@pytest.mark.asyncio
async def test_up_takes_the_newest_queued_steer_back_and_unread_steers_are_resent_at_turn_end():
    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _type(app, pilot, "one")
        await _type(app, pilot, "two")
        await _type(app, pilot, "three")
        assert await _until(pilot, lambda: sum(1 for c in be.calls if c[0] == "steer") == 2)
        ids = [c[2] for c in be.calls if c[0] == "steer"]
        assert len(app.screen.query(".steer-msg")) == 2 and "2 queued" in str(app.screen.query_one("#talk-status", Static).content)
        # up on the EMPTY composer pulls the newest one back to edit
        comp = app.screen.query_one("#composer", Input)
        comp.focus()
        assert comp.value == ""
        await pilot.press("up")
        assert await _until(pilot, lambda: ("steer_cancel", app.screen.convo.session_id, ids[1]) in be.calls)
        assert await _until(pilot, lambda: comp.value == "three")
        assert len(app.screen.query(".steer-msg")) == 1
        # the turn ends with "two" still queued (never folded in) → it is re-sent as a fresh turn
        be.pending_steers = [{"id": ids[0], "text": "two"}]
        comp.value = ""
        fake.hang = False
        await pilot.press("escape")  # CancelTask → the hanging stream yields canceled
        assert await _until(pilot, lambda: len(fake.sent) == 2)
        assert fake.sent[1]["text"] == "two"
        assert await _until(pilot, lambda: app.screen.convo.live is None and not app.screen.query(".steer-msg"))


@pytest.mark.asyncio
async def test_a_folded_in_steer_settles_on_its_frame_and_a_park_keeps_the_rest_queued():
    be = TalkBackend()

    class Consuming(FakeA2A):
        """Yields the task, waits for a steer to be queued, marks it consumed, then parks."""

        def stream(self, text, *, context_id, task_id=None, metadata=None):
            self.sent.append({"text": text, "context_id": context_id, "task_id": task_id, "metadata": metadata})
            yield {"result": {"task": {"id": "t1", "contextId": context_id, "status": {"state": "TASK_STATE_SUBMITTED"}}}}
            assert self._wait(lambda: sum(1 for c in be.calls if c[0] == "steer") >= 2), "steers never arrived"
            first = [c for c in be.calls if c[0] == "steer"][0]
            yield status(parts=[{"data": {"items": [{"id": first[2], "text": first[3]}]}, "metadata": {"mimeType": a2a.STEER_CONSUMED_MIME}}], cid=context_id)
            yield park_frame(context_id, {"question": "and now?"})

    fake = Consuming()
    be._a2a = fake
    be.steer_pending = lambda agent, sid: [{"id": c[2], "text": c[3]} for c in be.calls if c[0] == "steer"][1:]  # all but the first were never read
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _type(app, pilot, "go")
        await _type(app, pilot, "faster")
        await _type(app, pilot, "and quieter")
        assert await _until(pilot, lambda: app.screen.convo.parked is not None)
        await pilot.pause(0.4)  # the turn-end reconcile
        bubbles = [b.render().plain for b in app.screen.query(".steer-msg")]
        assert len(bubbles) == 2 and "folded in" in bubbles[0] and "queued" in bubbles[1]
        assert len(fake.sent) == 1  # a parked turn keeps the unread steer for after the answer


# ── interjecting into a server-fired turn, attaching to a turn in flight ──


@pytest.mark.asyncio
async def test_a_server_fired_turn_on_the_bus_is_attached_and_takes_interjections_when_controllable():
    sub = [
        {"result": {"task": {"id": "t7", "contextId": "SID", "status": {"state": "TASK_STATE_WORKING"}}}},
        status(parts=[{"text": "reading the board"}], cid="SID"),
        status("TASK_STATE_COMPLETED", final=True, cid="SID"),
    ]
    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        sid = app.screen.convo.session_id
        fake.sub_frames = [json.loads(json.dumps(f).replace("SID", sid)) for f in sub]
        fake.sub_frames[-1]["result"]["statusUpdate"]["taskId"] = "t7"
        for f in fake.sub_frames[1:]:
            f["result"]["statusUpdate"]["taskId"] = "t7"
        # the scheduler's own turn.started names only the session (no task id) — nothing to attach to yet
        fe.pending.append(ev("protoEngineer-ba4c", "turn.started", session_id=sid, origin="scheduler", trigger="daily-report"))
        await pilot.pause(0.7)
        assert fake.subscribed == [] and app.screen.convo.live is None
        # the chat.progress `turn_started` frame carries the task id and the control block: attach
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id=sid, task_id="t7", phase="turn_started", control={"operator_controllable": False, "origin": "scheduler", "trigger": "daily-report"}))
        assert await _until(pilot, lambda: fake.subscribed == ["t7"])
        assert await _until(pilot, lambda: app.screen.convo.live is not None and app.screen.convo.live.attached)
        live = app.screen.convo.live
        assert live.origin == "scheduler" and not live.controllable
        assert "(scheduler · daily-report turn)" in app.screen.query(".user-msg").last().render().plain
        st = str(app.screen.query_one("#talk-status", Static).content)
        assert "scheduler turn" in st and "not taking messages" in st
        # a message while it is not controllable is refused and stays in the composer
        comp = app.screen.query_one("#composer", Input)
        comp.value = "look at #12 too"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert comp.value == "look at #12 too" and not any(c[0] == "interject" for c in be.calls)
        # the bus says the operator may interject (attended session) → the composer interjects
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id=sid, task_id="t7", phase="tool_start", tool="read_board", tool_call_id="c1", control={"operator_controllable": True, "origin": "scheduler"}))
        assert await _until(pilot, lambda: app.screen.convo.live is not None and app.screen.convo.live.controllable)
        assert "type to interject" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("enter")
        assert await _until(pilot, lambda: any(c[0] == "interject" for c in be.calls))
        call = next(c for c in be.calls if c[0] == "interject")
        assert call[1] == sid and call[2] == "t7" and call[4] == "look at #12 too"
        assert "interjection" in app.screen.query(".steer-msg").first().render().plain
        await pilot.press("escape")  # detach (abort the subscription) — the server's turn goes on
        await _settle(app, pilot)
        assert fake.cancelled == [] and fake.aborted and live.detached and not live.live  # never CancelTask somebody else's turn
        assert not isinstance(app.screen, ConversationScreen)


@pytest.mark.asyncio
async def test_opening_a_session_with_a_turn_in_flight_attaches_to_it():
    sid = "chat-1700000000000-abc"
    row = {"task_id": "t9", "status": {"state": "TASK_STATE_WORKING"}, "history": [{"role": "ROLE_USER", "parts": [{"text": "from the console"}]}], "artifacts": []}
    sub = [{"result": {"task": {"id": "t9", "contextId": sid, "status": {"state": "TASK_STATE_WORKING"}}}}] + [
        {**f, "result": {"statusUpdate": {**f["result"]["statusUpdate"], "taskId": "t9"}}} if "statusUpdate" in f["result"] else ({**f, "result": {"artifactUpdate": {**f["result"]["artifactUpdate"], "taskId": "t9"}}} if "artifactUpdate" in f["result"] else f)
        for f in canned_frames(sid)[1:]
    ]
    fake = FakeA2A(sub_frames=sub)
    be = TalkBackend(a2a_client=fake, turns={sid: [row]})
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        app.open_member("protoEngineer-ba4c", sid)
        await _settle(app, pilot)
        assert isinstance(app.screen, ConversationScreen)
        assert await _until(pilot, lambda: fake.subscribed == ["t9"])
        assert await _until(pilot, lambda: app.screen.convo.latest is not None and app.screen.convo.latest.turn.done)
        ex = app.screen.convo.latest
        assert ex.attached and ex.origin == "in flight" and ex.user == "from the console"
        assert "Three PRs are open." in str(app.screen.query(Markdown).first().source)
        assert "◇ in flight" in str(app.screen.query(".turn-meta").first().content)
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)


@pytest.mark.asyncio
async def test_attaching_to_a_task_that_just_finished_finalizes_from_the_durable_record():
    """Live finding: SubscribeToTask on a finished task is refused with a JSON-RPC error
    ("already in a terminal state") — that is a completion to show, not a failure."""
    sid = "chat-1700000000000-fin"
    row = {"task_id": "t9", "status": {"state": "TASK_STATE_WORKING"}, "history": [{"role": "ROLE_USER", "parts": [{"text": "hi"}]}], "artifacts": []}
    fake = FakeA2A(sub_frames=[{"jsonrpc": "2.0", "id": "1", "error": {"code": -32602, "message": "Task t9 is in terminal state: 3"}}])
    be = TalkBackend(a2a_client=fake, turns={sid: [row]})
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        app.open_member("protoEngineer-ba4c", sid)
        await _settle(app, pilot)
        assert await _until(pilot, lambda: fake.subscribed == ["t9"] and app.screen.convo.latest is not None and app.screen.convo.latest.turn.done)
        ex = app.screen.convo.latest
        assert not ex.error and "finalized from the task" in str(app.screen.query(Markdown).first().source)
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)


# ── cancelling one delegation ──


@pytest.mark.asyncio
async def test_ctrl_x_on_a_running_task_card_cancels_that_delegation_only():
    TOOL = a2a.TOOL_CALL_EXT_URI
    frames = [
        {"result": {"task": {"id": "t1", "contextId": "s", "status": {"state": "TASK_STATE_SUBMITTED"}}}},
        status(metadata={TOOL: {"toolCallId": "d1", "name": "task", "phase": "started", "args": "review PR"}}),
        status(metadata={TOOL: {"toolCallId": "c2", "name": "run_command", "phase": "started", "args": "gh pr diff", "parentToolCallId": "d1"}}),
        status("TASK_STATE_COMPLETED", final=True),
    ]
    fake = FakeA2A(frames=None, hang=True)
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        sid = app.screen.convo.session_id
        fake.frames = [json.loads(json.dumps(f).replace('"s"', f'"{sid}"')) for f in frames]
        await _type(app, pilot, "review it")
        assert await _until(pilot, lambda: len(app.screen.query_one("#work-tree", Tree).root.children) == 1)
        # ctrl+x in the composer is the composer's cut — nothing happens to the delegation
        await pilot.press("ctrl+x")
        await pilot.pause(0.1)
        assert not any(c[0] == "delegation_cancel" for c in be.calls)
        await pilot.press("tab")  # → work tree; nothing selected yet, but exactly one task runs
        await pilot.pause(0.1)
        tree = app.screen.query_one("#work-tree", Tree)
        assert tree.has_focus and tree.cursor_node is None
        await pilot.press("ctrl+x")
        assert await _until(pilot, lambda: ("delegation_cancel", sid, "d1") in be.calls)
        assert not fake.cancelled  # the TURN was not cancelled
        # with the cursor on a NON-task card (the nested run_command) nothing is cancelled
        await pilot.press("down", "down")
        await pilot.pause(0.1)
        assert tree.cursor_node is not None and tree.cursor_node.data.name == "run_command"
        n = len(be.calls)
        await pilot.press("ctrl+x")
        await pilot.pause(0.2)
        assert len([c for c in be.calls if c[0] == "delegation_cancel"]) == 1 and len(be.calls) == n
        await pilot.press("escape")
        await _settle(app, pilot)


# ── attendance ──


@pytest.mark.asyncio
async def test_attendance_follows_the_open_session_and_is_released_on_leave():
    be = TalkBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        sid = app.screen.convo.session_id
        attends = [c for c in be.calls if c[0] == "attend"]
        assert len(attends) == 1 and attends[0][1] == sid and not attends[0][2].closed
        await pilot.press("ctrl+n")
        await pilot.pause(0.2)
        attends = [c for c in be.calls if c[0] == "attend"]
        assert len(attends) == 2 and attends[0][2].closed and attends[1][1] == app.screen.convo.session_id and not attends[1][2].closed
        await pilot.press("escape")
        await _settle(app, pilot)
        assert attends[1][2].closed


@pytest.mark.asyncio
async def test_offline_conversation_cannot_be_opened_so_nothing_to_act_on():
    be = TalkBackend(mode="offline")
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("j", "enter")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConversationScreen)
        assert not any(c[0] == "attend" for c in be.calls)


# ── the activity model's park / server-turn bookkeeping ──


def test_activity_tracks_parks_from_the_bus_and_the_live_server_turn_per_session():
    act = Activity({"x": "X"})
    # the scheduler's turn.started names only the session; the task id and the control
    # block arrive on the chat.progress `turn_started` frame (server/a2a.py)
    act.apply(ev("x", "turn.started", session_id="chat-1", origin="scheduler", trigger="daily"))
    assert act.server_turn("x", "chat-1") is None and act.turn_cell("x") == "⟳ running"
    act.apply(ev("x", "chat.progress", session_id="chat-1", task_id="t1", phase="turn_started", control={"operator_controllable": False, "origin": "scheduler", "trigger": "daily"}))
    assert act.server_turn("x", "chat-1") == {"task_id": "t1", "origin": "scheduler", "trigger": "daily", "controllable": False}
    act.apply(ev("x", "chat.progress", session_id="chat-1", task_id="t1", phase="tool_start", tool="t", tool_call_id="c", control={"operator_controllable": True, "origin": "scheduler", "trigger": "daily"}))
    assert act.server_turn("x", "chat-1")["controllable"] is True
    act.apply(ev("x", "turn.input_required", context_id="chat-1", task_id="t1", prompt="Approve shell command?"))
    assert act.turn_cell("x") == "⚑ needs you" and act.state["x"].parked["chat-1"].task_id == "t1"
    assert act.server_turn("x", "chat-1") is None  # parked → not addressable any more
    assert act.rows[-1].kind == "needs-you" and act.rows[-1].session == "chat-1" and act.ring_due() == [("x", "chat-1", "Approve shell command?")]
    act.apply(ev("x", "turn.resumed", context_id="chat-1", task_id="t1"))
    assert act.turn_cell("x") == "⟳ running" and act.state["x"].parked == {}  # answered: running again
    act.apply(ev("x", "turn.usage", task_id="t1", context_id="chat-1", state="TASK_STATE_COMPLETED"))
    assert act.turn_cell("x") == "idle"
    act.apply(ev("x", "chat.progress", session_id="chat-2", task_id="t2", phase="turn_started", control={"origin": "inbox"}))
    act.apply(ev("x", "turn.usage", task_id="t2", context_id="chat-2", state="TASK_STATE_COMPLETED"))
    assert act.server_turn("x", "chat-2") is None
    act.apply(ev("x", "chat.progress", session_id="chat-3", task_id="t3", phase="turn_started", control={"origin": "watch"}))
    act.apply(ev("x", "turn.finished", session_id="chat-3", task_id="t3", ok=True))
    assert act.server_turn("x", "chat-3") is None and act.server_turn("nobody", "chat-3") is None


# ── round-1 review reproducers (state-machine races) ──


@pytest.mark.asyncio
async def test_the_decks_own_park_on_the_bus_does_not_reload_the_session_under_the_modal():
    """Blocker: `turn.input_required` is published for EVERY context, the deck's own turn
    included. Reloading the durable turns then swapped the exchange out from under the open
    modal — the answer resumed an orphan, the screen stayed "needs you", and a second
    hitl_resume was one keypress away."""
    import time as _t

    fake = Parking({"kind": "approval", "title": "Approve?"})
    be = TalkBackend(a2a_client=fake)
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        sid = app.screen.convo.session_id
        be._turns[sid] = [{"task_id": "t1", "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"role": "ROLE_AGENT", "parts": [{"text": "Input required."}, {"data": {"kind": "approval", "title": "Approve?"}, "metadata": {"mimeType": a2a.HITL_MIME}}]}}, "history": [{"role": "ROLE_USER", "parts": [{"text": "clean"}]}], "artifacts": []}]
        await _send(app, pilot, "clean")
        convo_screen = app.screen
        old = convo_screen.convo.parked
        reads = len(be.turn_reads)
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ApprovalModal)
        fe.pending.append(ev("protoEngineer-ba4c", "turn.input_required", context_id=sid, task_id="t1", prompt="Approve?"))
        await pilot.pause(0.8)
        assert convo_screen.convo.parked is old and len(be.turn_reads) == reads  # known park: no reload
        await pilot.press("a")
        assert await _until(pilot, lambda: len(fake.sent) == 2)
        assert await _until(pilot, lambda: convo_screen.convo.live is None and convo_screen.convo.parked is None)
        assert "idle" in str(convo_screen.query_one("#talk-status", Static).content)
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConversationScreen) and len(fake.sent) == 2  # nothing to answer twice
        # a park we did NOT watch happen (another client's turn in this session) still reloads
        be._turns[sid].append({"task_id": "t2", "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"role": "ROLE_AGENT", "parts": [{"text": "Which?"}]}}, "history": [{"role": "ROLE_USER", "parts": [{"text": "from the console"}]}], "artifacts": []})
        fe.pending.append(ev("protoEngineer-ba4c", "turn.input_required", context_id=sid, task_id="t2", prompt="Which?"))
        assert await _until(pilot, lambda: len(be.turn_reads) == reads + 1)
        assert await _until(pilot, lambda: app.screen.convo.parked is not None and app.screen.convo.parked.turn.task_id == "t2")
        _t.sleep(0)


@pytest.mark.asyncio
async def test_a_stall_finalize_finishes_once_and_resends_an_unread_steer_once(monkeypatch):
    """Major: the stall probe and the unwinding reader both reached `_finish`; each ran the
    steer reconcile, so an unread steer was re-sent twice as two turns."""
    from deck import talk as talkmod

    monkeypatch.setattr(talkmod, "STALL_IDLE_S", 0.2)
    fake = FakeA2A(block_after=3)
    be = TalkBackend(a2a_client=fake)
    finishes: list = []
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        scr = app.screen
        orig = scr._finish_render
        scr._finish_render = lambda ex, err: (finishes.append(err), orig(ex, err))  # type: ignore[method-assign]
        await _type(app, pilot, "go")
        await _type(app, pilot, "later")
        assert await _until(pilot, lambda: any(c[0] == "steer" for c in be.calls))
        st_id = [c[2] for c in be.calls if c[0] == "steer"][0]
        be.pending_steers = [{"id": st_id, "text": "later"}]
        await pilot.pause(0.3)
        fake.block_after = None
        scr._check_stall()
        await _settle(app, pilot)
        await pilot.pause(0.8)
        await _settle(app, pilot)
        assert [s["text"] for s in fake.sent] == ["go", "later"]
        assert sum(1 for c in be.calls if c[0] == "steer_pending") == 1
        assert finishes.count("") >= 1 and len(scr.convo.exchanges) == 2
        assert not scr.convo.exchanges[0].error  # #4: the reader must not fail a turn the probe finalized


@pytest.mark.asyncio
async def test_a_stall_finalize_marks_the_turn_done_before_waking_the_reader(monkeypatch):
    """Major: abort() before `done` let the reader win the race and end a completed turn as
    "✗ stream closed". Slowing the finalizer forces that ordering."""
    import time as _t

    from deck import talk as talkmod

    monkeypatch.setattr(talkmod, "STALL_IDLE_S", 0.2)
    orig_abort = FakeA2A.abort

    def slow_abort(self):
        orig_abort(self)
        _t.sleep(0.15)  # the reader gets the GIL first — the ordering the old code assumed it never loses

    monkeypatch.setattr(FakeA2A, "abort", slow_abort)
    fake = FakeA2A(block_after=3)
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _type(app, pilot, "go", wait=0.4)
        app.screen._check_stall()
        await _settle(app, pilot)
        await pilot.pause(0.5)
        ex = app.screen.convo.exchanges[-1]
        assert ex.turn.done and not ex.error and "✗" not in str(app.screen.query(".turn-meta").first().content)


@pytest.mark.asyncio
async def test_a_reconcile_that_lands_after_a_session_switch_or_an_answer_sends_nothing():
    """Major: the turn-end reconcile re-sent leftover steers into whatever session existed
    when its roundtrip returned — the NEW session after ctrl+n, or as a second turn while a
    typed answer had already resumed the parked one."""
    import time as _t

    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    orig = be.steer_pending

    def slow_pending(agent, sid):
        _t.sleep(0.6)
        return orig(agent, sid)

    be.steer_pending = slow_pending
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _type(app, pilot, "one")
        await _type(app, pilot, "two")
        assert await _until(pilot, lambda: any(c[0] == "steer" for c in be.calls))
        ids = [c[2] for c in be.calls if c[0] == "steer"]
        be.pending_steers = [{"id": ids[0], "text": "two"}]
        fake.hang = False
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.screen.convo.live is None)
        await pilot.press("ctrl+n")
        await pilot.pause(0.1)
        await pilot.pause(1.0)
        assert len(fake.sent) == 1  # nothing re-sent into the new session
    # …and an answer typed inside the roundtrip: the stale reconcile is dropped; the resumed
    # turn folds the held steer in (the server's queue drains at its next model call)
    be2 = TalkBackend()

    class ParksAfterASteer(Parking):
        def stream(self, text, *, context_id, task_id=None, metadata=None):
            self.sent.append({"text": text, "context_id": context_id, "task_id": task_id, "metadata": metadata})
            if metadata and metadata.get("hitl_resume"):
                yield from canned_frames(context_id)
                return
            yield {"result": {"task": {"id": "t1", "contextId": context_id, "status": {"state": "TASK_STATE_SUBMITTED"}}}}
            assert self._wait(lambda: any(c[0] == "steer" for c in be2.calls)), "the steer never arrived"
            yield park_frame(context_id, self.hitl)

    fake2 = ParksAfterASteer({"question": "and now?"})
    be2._a2a = fake2

    def slow_pending2(agent, sid):
        _t.sleep(0.6)
        answered = any(s["metadata"] for s in fake2.sent)
        return [] if answered else [{"id": c[2], "text": c[3]} for c in be2.calls if c[0] == "steer"]

    be2.steer_pending = slow_pending2
    app2 = FleetDeck(be2, poll_s=0)
    async with app2.run_test(size=(120, 36)) as pilot:
        await _open_talk(be2, pilot, app2)
        await _type(app2, pilot, "go", wait=0.1)
        await _type(app2, pilot, "faster", wait=0.1)  # queued into the running turn, which then parks
        assert await _until(pilot, lambda: app2.screen.convo.parked is not None)
        await _type(app2, pilot, "yes", wait=0.1)  # answers while reconcile #1's roundtrip is still out
        await pilot.pause(1.5)
        assert [s["text"] for s in fake2.sent] == ["go", "yes"]  # "faster" was never re-sent as a turn of its own
        assert [st.consumed for st in app2.screen.convo.steers] == [True]  # reconcile #2 found it folded in


@pytest.mark.asyncio
async def test_a_form_field_that_reveals_a_sibling_keeps_focus_and_the_caret():
    """Major: revealing a `showWhen` sibling re-rendered the step and dropped focus onto
    the scroll container — every following keystroke was lost."""
    steps = [{"schema": {"properties": {"name": {"type": "string"}, "tag": {"type": "string", "showWhen": {"field": "name"}}}, "required": ["name"]}}]
    fake = Parking({"kind": "form", "title": "F", "steps": steps})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "go")
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        modal = app.screen
        assert isinstance(modal, FormModal) and getattr(modal.focused, "id", None) == "in-name"
        await pilot.press("b")
        await pilot.pause(0.3)
        assert getattr(modal.focused, "id", None) == "in-name" and modal.query("#field-tag")
        await pilot.press(*"ob")
        await pilot.pause(0.2)
        assert modal.query_one("#in-name", Input).value == "bob" and modal.values["name"] == "bob"
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert json.loads(_resume_call(fake)["text"]) == {"name": "bob"}


@pytest.mark.asyncio
async def test_an_attached_server_turn_is_not_reported_twice_to_the_feed_and_a_replaced_attach_replays_once():
    fake = FakeA2A()
    be = TalkBackend(a2a_client=fake)
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    TOOL = a2a.TOOL_CALL_EXT_URI
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        sid = app.screen.convo.session_id

        def su(meta=None, state="TASK_STATE_WORKING", final=False):
            msg = {"role": "ROLE_AGENT", "messageId": "m", "parts": []}
            if meta:
                msg["metadata"] = meta
            return {"result": {"statusUpdate": {"taskId": "t7", "contextId": sid, "status": {"state": state, "message": msg}, "final": final}}}

        fake.sub_frames = [
            {"result": {"task": {"id": "t7", "contextId": sid, "status": {"state": "TASK_STATE_WORKING"}}}},
            su({TOOL: {"toolCallId": "c1", "name": "read_board", "phase": "started", "args": "x"}}),
            su({TOOL: {"toolCallId": "c1", "name": "read_board", "phase": "completed", "result": "ok"}}),
            su(state="TASK_STATE_COMPLETED", final=True),
        ]
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id=sid, task_id="t7", phase="turn_started", control={"origin": "scheduler"}))
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id=sid, task_id="t7", phase="tool_start", tool="read_board", tool_call_id="c1", control={"origin": "scheduler"}))
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id=sid, task_id="t7", phase="tool_end", tool="read_board", tool_call_id="c1", output="ok", control={"origin": "scheduler"}))
        assert await _until(pilot, lambda: app.screen.convo.latest is not None and app.screen.convo.latest.turn.done)
        await pilot.pause(0.3)
        assert [(r.glyph, r.label) for r in app.activity.rows if r.tool_id == "c1"] == [("⟳", "read_board"), ("✓", "read_board")]
    # a turn in flight when the session opens: the durable copy is replaced by the snapshot, not doubled
    sid2 = "chat-1700000000000-abc"
    hist = [{"role": "ROLE_USER", "parts": [{"text": "q"}]}, {"role": "ROLE_AGENT", "parts": [{"data": {"text": "thinking hard"}, "metadata": {"mimeType": a2a.REASONING_MIME}}]}]
    row = {"task_id": "t9", "status": {"state": "TASK_STATE_WORKING"}, "history": hist, "artifacts": []}
    sub = [
        {"result": {"task": {"id": "t9", "contextId": sid2, "status": {"state": "TASK_STATE_WORKING"}, "history": hist, "artifacts": []}}},
        {"result": {"statusUpdate": {"taskId": "t9", "contextId": sid2, "status": {"state": "TASK_STATE_COMPLETED"}, "final": True}}},
    ]
    fake2 = FakeA2A(sub_frames=sub)
    be2 = TalkBackend(a2a_client=fake2, turns={sid2: [row]})
    app2 = FleetDeck(be2, poll_s=0)
    async with app2.run_test(size=(120, 36)) as pilot:
        await _settle(app2, pilot)
        app2.open_member("protoEngineer-ba4c", sid2)
        await _settle(app2, pilot)
        assert await _until(pilot, lambda: app2.screen.convo.latest is not None and app2.screen.convo.latest.turn.done)
        assert app2.screen.convo.latest.turn.reasoning == "thinking hard"


@pytest.mark.asyncio
async def test_attendance_that_gave_up_is_said_once():
    be = TalkBackend()
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        h = [c for c in be.calls if c[0] == "attend"][-1][2]
        h.gave_up, h.last_error = True, "HTTP 404: Not Found"
        seen: list = []
        app.screen.notify = lambda msg, **kw: seen.append(msg)  # type: ignore[method-assign]
        app.screen._check_stall()
        app.screen._check_stall()
        assert len([m for m in seen if "NOT attended" in m]) == 1



# ── round-2 review reproducers ──


@pytest.mark.asyncio
async def test_a_park_answered_elsewhere_follows_the_bus_instead_of_sending_a_stale_resume():
    """Blocker: the deck kept its own park after the bus said the console answered it; the
    operator then sent a hitl_resume to a task no longer parked (an ordinary turn on the
    server) and every re-render re-parked the roster and re-rang the bell."""
    fake = Parking({"kind": "approval", "title": "Approve shell command?"})
    be = TalkBackend(a2a_client=fake)
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    rings: list = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        sid = app.screen.convo.session_id
        fake.sub_frames = [json.loads(json.dumps(f).replace('"s"', f'"{sid}"')) for f in canned_frames("s")]
        await _send(app, pilot, "clean the build")
        assert app.screen.convo.parked is not None and app.activity.turn_cell("protoEngineer-ba4c") == "⚑ needs you"
        rings.clear()
        # the console answers the same park: the member publishes turn.resumed for t1 and runs on
        fe.pending.append(ev("protoEngineer-ba4c", "turn.resumed", context_id=sid, task_id="t1"))
        assert await _until(pilot, lambda: fake.subscribed == ["t1"])  # the deck follows the continued turn
        assert await _until(pilot, lambda: app.screen.convo.live is None and app.screen.convo.latest.turn.done)
        assert app.screen.convo.parked is None and "idle" in str(app.screen.query_one("#talk-status", Static).content)
        assert "Three PRs are open." in str(app.screen.query(Markdown).first().source)
        await pilot.press("ctrl+z")  # a re-render must not re-park the roster
        fe.pending.append(ev("protoEngineer-ba4c", "turn.usage", task_id="t1", context_id=sid, state="TASK_STATE_COMPLETED"))
        await pilot.pause(0.8)
        assert app.activity.turn_cell("protoEngineer-ba4c") == "idle" and rings == []
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConversationScreen) and len(fake.sent) == 1  # nothing to answer, nothing sent
    # …and a park that ENDED elsewhere (terminal usage / finished) shows how it ended
    fake2 = Parking({"question": "Merge?"})
    be2 = TalkBackend(a2a_client=fake2)
    fe2 = FakeEvents()
    app2 = FleetDeck(be2, poll_s=0, events=fe2)
    async with app2.run_test(size=(120, 36)) as pilot:
        await _open_talk(be2, pilot, app2)
        sid = app2.screen.convo.session_id
        await _send(app2, pilot, "ship it")
        assert app2.screen.convo.parked is not None
        be2._turns[sid] = [{"task_id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "history": [{"role": "ROLE_USER", "parts": [{"text": "ship it"}]}], "artifacts": [{"parts": [{"text": "Shipped from the console."}]}]}]
        fe2.pending.append(ev("protoEngineer-ba4c", "turn.usage", task_id="t1", context_id=sid, state="TASK_STATE_COMPLETED", cost_usd=0.1))
        assert await _until(pilot, lambda: app2.screen.convo.parked is None and app2.screen.convo.latest is not None and app2.screen.convo.latest.turn.done)
        assert await _until(pilot, lambda: "Shipped from the console." in str(app2.screen.query(Markdown).first().source))
        assert len(fake2.sent) == 1
        # a modal that was open across the answer refuses to send and hands the text back
        fake3 = Parking({"question": "Which?"})
        be3 = TalkBackend(a2a_client=fake3)
    fe3 = FakeEvents()
    app3 = FleetDeck(be3, poll_s=0, events=fe3)
    async with app3.run_test(size=(120, 36)) as pilot:
        await _open_talk(be3, pilot, app3)
        sid = app3.screen.convo.session_id
        await _send(app3, pilot, "pick")
        convo_screen = app3.screen
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert isinstance(app3.screen, QuestionModal)
        await pilot.press(*"main")
        be3._turns[sid] = [{"task_id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}, "history": [], "artifacts": [{"parts": [{"text": "took develop"}]}]}]
        fe3.pending.append(ev("protoEngineer-ba4c", "turn.finished", session_id=sid, task_id="t1", ok=True))
        await pilot.pause(0.8)
        await pilot.press("enter")
        await _settle(app3, pilot)
        assert len(fake3.sent) == 1 and convo_screen.query_one("#composer", Input).value == "main"


@pytest.mark.asyncio
async def test_a_server_turn_the_bus_showed_continues_its_in_flight_durable_row():
    sid = "chat-1700000000000-srv"
    row = {"task_id": "t7", "status": {"state": "TASK_STATE_WORKING"}, "history": [{"role": "ROLE_USER", "parts": [{"text": "daily report"}]}], "artifacts": [{"parts": [{"text": "so far"}]}]}
    sub = [
        {"result": {"task": {"id": "t7", "contextId": sid, "status": {"state": "TASK_STATE_WORKING"}, "history": row["history"], "artifacts": row["artifacts"]}}},
        {"result": {"artifactUpdate": {"taskId": "t7", "contextId": sid, "artifact": {"artifactId": "a", "parts": [{"text": "so far, and done."}]}, "lastChunk": True}}},
        {"result": {"statusUpdate": {"taskId": "t7", "contextId": sid, "status": {"state": "TASK_STATE_COMPLETED"}, "final": True}}},
    ]
    fake = FakeA2A(sub_frames=sub)
    be = TalkBackend(a2a_client=fake, turns={sid: [row]})
    fe = FakeEvents()
    app = FleetDeck(be, poll_s=0, events=fe)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(app, pilot)
        fe.pending.append(ev("protoEngineer-ba4c", "chat.progress", session_id=sid, task_id="t7", phase="turn_started", control={"origin": "scheduler", "trigger": "daily", "operator_controllable": True}))
        await pilot.pause(0.7)
        app.open_member("protoEngineer-ba4c", sid)
        await _settle(app, pilot)
        assert await _until(pilot, lambda: fake.subscribed == ["t7"] and app.screen.convo.live is None)
        exs = app.screen.convo.exchanges
        assert [e.turn.task_id for e in exs] == ["t7"] and exs[0].turn.done and exs[0].controllable
        assert "1 turn" in str(app.screen.query_one("#talk-head", Static).content) and len(app.screen.query(Markdown)) == 1
        assert "so far, and done." in str(app.screen.query(Markdown).first().source)


@pytest.mark.asyncio
async def test_a_plugin_form_cannot_be_reopened_while_its_submit_is_in_flight():
    """Blocker: re-opening the form during the submit roundtrip lost the second set of
    answers and sent them to the A2A task as a hitl_resume carrying a dict repr."""
    import time as _t

    fake = Parking({"kind": "form", "title": "Post?", "plugin_callback_id": "cb1", "steps": [{"schema": {"properties": {"text": {"type": "string"}}, "required": ["text"]}}]})
    be = TalkBackend(a2a_client=fake)
    orig = be.submit_form

    def slow_submit(agent, sid, cb, answers):
        _t.sleep(0.9)
        return orig(agent, sid, cb, answers)

    be.submit_form = slow_submit  # type: ignore[method-assign]
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "post the update")
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        assert isinstance(app.screen, FormModal)
        app.screen.query_one("#in-text", Input).focus()
        await pilot.press(*"hello", "ctrl+s")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConversationScreen) and "submitting the form" in str(app.screen.query_one("#talk-status", Static).content)
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ConversationScreen)  # refused while in flight
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ConversationScreen)
        assert await _until(pilot, lambda: app.screen.convo.parked is None, timeout=3)
        assert "idle" in str(app.screen.query_one("#talk-status", Static).content)
        assert len([c for c in be.calls if c[0] == "submit_form"]) == 1 and len(fake.sent) == 1



@pytest.mark.asyncio
async def test_form_keys_that_are_not_valid_widget_ids_still_render_and_round_trip():
    """CodeRabbit: a schema key with a dot, a colon, a space or a leading digit is not a
    Textual id — the modal must not raise, and the answers keep the original keys."""
    steps = [{"schema": {"properties": {"user.name": {"type": "string"}, "1st": {"type": "string"}, "a b": {"type": "boolean"}, "x:y": {"enum": ["p", "q"]}}, "required": ["user.name"]}}]
    fake = Parking({"kind": "form", "title": "Odd keys", "steps": steps})
    be = TalkBackend(a2a_client=fake)
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "go")
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        modal = app.screen
        assert isinstance(modal, FormModal) and len(modal.query(".hitl-field")) == 4
        assert modal.query_one("#in-user_name", Input) and modal.query_one("#in-f_1st", Input)
        modal.query_one("#in-user_name", Input).focus()
        await pilot.press(*"kj")
        modal.query_one("#in-f_1st", Input).focus()
        await pilot.press("z")
        modal.query_one("#in-x_y", Select).value = "q"
        await pilot.pause(0.2)
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert json.loads(_resume_call(fake)["text"]) == {"user.name": "kj", "1st": "z", "x:y": "q"}


@pytest.mark.asyncio
async def test_a_steer_whose_enqueue_is_in_flight_when_the_turn_ends_is_not_marked_folded_in():
    """CodeRabbit: the reconcile ran before the steer POST returned, found it absent from the
    member's queue and marked it consumed — while the POST then queued it after the turn.
    An in-flight enqueue is not judged; once accepted it is reconciled on its own."""
    import time as _t

    fake = FakeA2A(hang=True)
    be = TalkBackend(a2a_client=fake)
    orig_steer = be.steer

    def slow_steer(agent, sid, msg_id, text):
        _t.sleep(0.7)
        return orig_steer(agent, sid, msg_id, text)

    be.steer = slow_steer  # type: ignore[method-assign]
    be.steer_pending = lambda agent, sid: [{"id": c[2], "text": c[3]} for c in be.calls if c[0] == "steer"]  # the member holds whatever was accepted
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 36)) as pilot:
        await _open_talk(be, pilot, app)
        await _type(app, pilot, "go", wait=0.2)
        await _type(app, pilot, "later", wait=0.1)  # its POST is out for 0.7 s
        assert "sending" in app.screen.query(".steer-msg").first().render().plain
        fake.hang = False
        await pilot.press("escape")  # the turn ends while the enqueue is still out
        assert await _until(pilot, lambda: app.screen.convo.live is None)
        assert not any(c[0] == "steer_pending" for c in be.calls)  # nothing to judge yet
        assert "folded in" not in app.screen.query(".steer-msg").first().render().plain
        # the POST lands, the member now holds it, the turn is over → it becomes a turn of its own
        assert await _until(pilot, lambda: len(fake.sent) == 2, timeout=4)
        assert fake.sent[1]["text"] == "later" and not app.screen.query(".steer-msg")


@pytest.mark.asyncio
async def test_a_plugin_form_with_no_fields_is_redeemed_as_an_empty_form():
    """CodeRabbit: `steps: []` used to open the question modal, whose typed text was
    discarded and the callback redeemed with {} anyway — now it is the form it is."""
    fake = Parking({"kind": "form", "title": "Confirm?", "plugin_callback_id": "cb1", "steps": []})
    be = TalkBackend(a2a_client=fake)
    be.form_result = {"reply": "done"}
    app = FleetDeck(be, poll_s=0)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_talk(be, pilot, app)
        await _send(app, pilot, "confirm")
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        assert isinstance(app.screen, FormModal) and not app.screen.query(".hitl-field")
        assert not app.screen.query_one("#submit", Button).disabled
        await pilot.press("ctrl+s")
        assert await _until(pilot, lambda: any(c[0] == "submit_form" and c[2] == "cb1" and c[3] == {} for c in be.calls))
        assert len(fake.sent) == 1
