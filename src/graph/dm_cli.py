"""``protoagent dm`` — direct-message a running agent from outside its chat.

    protoagent dm mythxEngineer "PR #868 no longer touches the chat harness" --from claude-code

The note goes to the agent's inbox with thread delivery (``POST /api/inbox`` with
``deliver: "thread"``). The agent's operator chat folds it in at its next model call
(``graph/middleware/outside_messages.py``): mid-turn when it is working, otherwise at the
start of its next turn. It is delivered as information only, and the agent is told to
mention it to the operator, so both of them have a record that it arrived.

This is not chat: nothing is streamed back and no turn is started. It exists so another
session on the same box (a Claude Code terminal, a script) has a sanctioned way to say
"here is what I am doing" to an agent, using the same hub discovery and credential chain
as ``protoagent fleet`` / ``protoagent usage``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from urllib.parse import quote

from deck import hub as deckhub
from deck.data import display_name, slug_of

MAX_CHARS = 8000
# One long-poll request; under the hub client's 5s read timeout.
_POLL_S = 3.0
# How many of the newest threads `--threads` asks for a busy signal.
_BUSY_PROBE = 8


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protoagent dm",
        description=(
            "Send a running agent a direct message. Its operator chat reads it at the next model call, "
            "as information only (it cannot authorise anything), and tells the operator."
        ),
    )
    p.add_argument("agent", help="the agent's name, label or id (see `protoagent fleet ls`); `host` is the hub itself")
    p.add_argument("message", nargs="?", help="the note; omit or pass - to read it from stdin")
    p.add_argument(
        "--from",
        dest="source",
        metavar="NAME",
        help="who is speaking, shown to the agent and the operator (e.g. claude-code:mythxengine)",
    )
    p.add_argument(
        "--thread",
        metavar="ID",
        help="address ONE chat thread (an id from --threads); default: whichever chat is active next",
    )
    p.add_argument(
        "--threads",
        action="store_true",
        help="do not send: list the agent's recent chat threads, newest first",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait up to SECONDS for the agent's reply and print it (exit 4 if none arrives in time)",
    )
    p.add_argument(
        "--check",
        type=int,
        metavar="ID",
        help="do not send: show the state of message ID and the agent's reply, if any (combine with --wait)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="emit the result as JSON")
    p.add_argument("--hub", metavar="URL", help="hub/server to use (default: discover the one running on this box)")
    p.add_argument(
        "--token",
        help=f"credential for --hub — a fleet token or operator bearer (env: {deckhub.ENV_TOKEN}); never printed",
    )
    p.add_argument(
        "--insecure-http",
        dest="insecure_http",
        action="store_true",
        help="allow sending a credential to a non-loopback http:// --hub (a network you trust)",
    )
    return p


def resolve_slug(roster: list[dict], wanted: str) -> tuple[str | None, list[str]]:
    """``(slug, candidates)`` for ``wanted``. Exact id first, then a case-insensitive match
    on name or label, then a case-insensitive id prefix (``protoEngineer`` finds the agent
    whose id is ``protoEngineer-ba4c`` even when its label is ``projectManager``). ``slug``
    is ``None`` when nothing or more than one row matches; the candidates are then the
    names worth showing the caller."""
    want = wanted.strip()
    if want.lower() == "host":
        return "host", []
    for row in roster:
        if slug_of(row) == want:
            return want, []
    low = want.lower()
    hits = [
        row
        for row in roster
        if low in {str(row.get("name") or "").lower(), str(row.get("label") or "").lower(), slug_of(row).lower()}
    ]
    if len(hits) == 1:
        return slug_of(hits[0]), []
    if not hits:
        # A workspace id is `<name>-<suffix>`, so the name an operator remembers is its prefix.
        hits = [row for row in roster if slug_of(row).lower().startswith(low)]
        if len(hits) == 1:
            return slug_of(hits[0]), []
    pool = hits or roster
    return None, sorted({display_name(r) for r in pool if display_name(r)})


def _fail(args: argparse.Namespace, message: str, code: int = 1) -> int:
    if args.as_json:
        print(json.dumps({"ok": False, "error": message}))
    else:
        print(f"✗ protoagent dm: {message}", file=sys.stderr)
    return code


def _await_reply(client, slug: str, item_id: int, wait: float) -> dict:
    """The item as it stands after waiting up to ``wait`` seconds for a reply. Each request
    long-polls briefly (well inside the client's read timeout) and is repeated until the
    deadline, so a long wait survives a proxy that would cut one long connection."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        left = deadline - time.monotonic()
        body = client.member_get(slug, f"/api/inbox/{int(item_id)}", wait=f"{max(0.0, min(left, _POLL_S)):.1f}")
        item = body.get("item") if isinstance(body, dict) else None
        if not isinstance(item, dict):
            raise deckhub.HubError(client.url, "the agent answered without the message")
        if item.get("reply_text") or deadline - time.monotonic() <= 0:
            return item


def _report(args: argparse.Namespace, slug: str, item: dict, *, waited: bool) -> int:
    """Print a message's state and reply. Exit 0 with a reply (or when not waiting), 4 when
    a wait ran out with none."""
    reply = item.get("reply_text") or ""
    read_by = item.get("delivered_session") or ""
    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "agent": slug,
                    "id": item.get("id"),
                    "read": bool(item.get("delivered_at")),
                    "read_by": read_by or None,
                    "reply": reply or None,
                    "replied_at": item.get("replied_at"),
                }
            )
        )
    elif reply:
        print(f"↩ {args.agent} replied to #{item.get('id')}:\n{reply}")
    elif read_by:
        print(f"· #{item.get('id')} was read by {args.agent} ({read_by}); no reply yet")
    else:
        print(f"· #{item.get('id')} is waiting: {args.agent}'s chat has not reached a model call since it arrived")
    return 4 if waited and not reply else 0


def run_dm_cli(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.threads:
        return _run_threads(parser, args)
    if args.check is not None:
        return _run_check(parser, args)
    if not (args.source or "").strip():
        parser.error("--from is required when sending")
    return _run_send(parser, args)


def _run_threads(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.message is not None or args.check is not None:
        parser.error("--threads only lists; drop the message / --check")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        conn = deckhub.connect(url=args.hub, token=args.token, insecure_http=args.insecure_http)
    except deckhub.HubError as exc:
        return _fail(args, f"needs a running server — start one with `protoagent up` ({exc})")
    try:
        slug, candidates = resolve_slug(conn.roster, args.agent)
        if slug is None:
            return _fail(args, f"no single agent matches {args.agent!r} (agents here: {', '.join(candidates) or 'none'})", 2)
        try:
            body = conn.client.member_get(slug, "/api/chat/sessions", limit=20)
        except deckhub.HubError as exc:
            return _fail(args, f"could not list {args.agent}'s chat threads ({exc})")
        rows = [r for r in (body.get("sessions") or []) if isinstance(r, dict)] if isinstance(body, dict) else []
        threads = [
            {"id": r.get("session_id"), "last_active": r.get("last_updated"), "turns": r.get("turn_count"), "working": None}
            for r in rows
            if r.get("session_id")
        ]
        # The busy signal is per thread; ask for the newest few (the ones worth addressing).
        for t in threads[:_BUSY_PROBE]:
            try:
                one = conn.client.member_get(slug, f"/api/chat/sessions/{quote(str(t['id']), safe='')}")
                t["working"] = bool(one.get("active")) if isinstance(one, dict) else None
            except deckhub.HubError:
                t["working"] = None  # unknown beats a wrong "idle"
    finally:
        conn.client.close()
    if args.as_json:
        print(json.dumps({"ok": True, "agent": slug, "threads": threads}))
    elif not threads:
        print(f"{args.agent} has no chat threads yet")
    else:
        print(f"{args.agent}'s chat threads, newest first (use one with --thread):")
        for t in threads:
            state = {True: "working now", False: "idle", None: ""}[t["working"]]
            print(f"  {t['id']}   last active {t['last_active'] or '?'}   {t['turns'] or 0} turn(s)   {state}".rstrip())
    return 0


def _run_check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.message is not None:
        parser.error("--check does not send; drop the message")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        conn = deckhub.connect(url=args.hub, token=args.token, insecure_http=args.insecure_http)
    except deckhub.HubError as exc:
        return _fail(args, f"needs a running server — start one with `protoagent up` ({exc})")
    try:
        slug, candidates = resolve_slug(conn.roster, args.agent)
        if slug is None:
            return _fail(args, f"no single agent matches {args.agent!r} (agents here: {', '.join(candidates) or 'none'})", 2)
        try:
            item = _await_reply(conn.client, slug, args.check, args.wait)
        except deckhub.HubError as exc:
            return _fail(args, f"could not read message {args.check} on {args.agent} ({exc})")
    finally:
        conn.client.close()
    return _report(args, slug, item, waited=args.wait > 0)


def _run_send(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    text = args.message
    if text is None or text == "-":
        text = sys.stdin.read()
    text = (text or "").strip()
    source = (args.source or "").strip()
    if not text:
        parser.error("the message is empty")
    if not source:
        parser.error("--from needs a name")
    if len(text) > MAX_CHARS:
        parser.error(f"the message is {len(text)} characters; keep it under {MAX_CHARS} (link to the detail instead)")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        conn = deckhub.connect(url=args.hub, token=args.token, insecure_http=args.insecure_http)
    except deckhub.HubError as exc:
        return _fail(args, f"needs a running server — start one with `protoagent up` ({exc})")

    try:
        slug, candidates = resolve_slug(conn.roster, args.agent)
        if slug is None:
            known = ", ".join(candidates) or "none"
            return _fail(args, f"no single agent matches {args.agent!r} (agents here: {known})", 2)
        try:
            payload = {"text": text, "source": source, "deliver": "thread"}
            if (args.thread or "").strip():
                payload["session"] = args.thread.strip()
            body = conn.client.member_post(slug, "/api/inbox", payload)
        except deckhub.HubError as exc:
            return _fail(args, f"{args.agent} did not accept the note ({exc})")
        return _after_send(args, conn.client, slug, source, body)
    finally:
        conn.client.close()


def _after_send(args: argparse.Namespace, client, slug: str, source: str, body) -> int:
    item = body.get("item") if isinstance(body, dict) else None
    if not isinstance(body, dict) or not body.get("ok") or not (item or body.get("deduped")):
        return _fail(args, f"{args.agent} answered without accepting the note")
    # An agent older than this command stores the note but ignores `deliver`, so it would
    # sit in the pull queue unread. Say so rather than report a delivery that won't happen.
    if item and item.get("delivery") != "thread":
        return _fail(
            args,
            f"{args.agent} is running a version that cannot deliver a note to its chat; "
            "it was left in the agent's inbox for a check_inbox pull instead",
            3,
        )
    item_id = (item or {}).get("id")
    if args.wait > 0 and item_id is not None:
        if not args.as_json:
            print(f"✓ direct message #{item_id} sent to {args.agent} from {source}; waiting up to {args.wait:g}s for a reply")
        try:
            latest = _await_reply(client, slug, item_id, args.wait)
        except deckhub.HubError as exc:
            return _fail(args, f"sent as #{item_id}, but could not wait for the reply ({exc})")
        return _report(args, slug, latest, waited=True)
    if args.as_json:
        print(json.dumps({"ok": True, "agent": slug, "id": item_id, "deduped": bool(body.get("deduped"))}))
    else:
        print(
            f"✓ direct message #{item_id} sent to {args.agent} from {source}; "
            f"{'thread ' + args.thread.strip() if (args.thread or '').strip() else 'its chat'} reads it at the next "
            f"model call (reply: protoagent dm {args.agent} --check {item_id})"
        )
    return 0
