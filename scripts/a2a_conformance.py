#!/usr/bin/env python3
"""A2A 1.0 conformance prober — point it at any A2A agent and get a report.

    python scripts/a2a_conformance.py --url http://127.0.0.1:7870
    python scripts/a2a_conformance.py --url https://peer.example/a2a --token $TOK --json

Why this exists
───────────────
A2A's interesting failure modes are *silent*. A 0.3-era client sends a
well-formed request to a 1.0 server and gets ``-32601 Method not found`` — not
because the method is missing, but because it omitted the ``A2A-Version: 1.0``
header, so the server read it as 0.3. A consumer that routes SSE frames on 0.3's
``kind`` discriminator skips every 1.0 frame and simply never attaches. Neither
looks like a protocol error; both look like "the agent is broken".

So rather than assert conformance in prose, this probes the wire and reports what
the peer actually does. It is deliberately **stdlib-only** — no ``a2a-sdk``, no
``httpx``, not even this repo on the path — so you can copy this single file next
to any agent, in any project, and run it.

What it checks
──────────────
  card       the agent card is reachable and carries the 1.0 required fields
  version    ``A2A-Version`` negotiation, including the silent-failure mode
  methods    which of the 11 A2A 1.0 JSON-RPC methods the peer serves
  compat     whether v0.3 method aliases are also mounted
  stream     SSE frame shape — the 1.0 oneof, and the ``append`` replace trap
  ext        extensions declared on the card vs. actually emitted on the wire
  lifecycle  GetTask + SubscribeToTask driven against a REAL task
  push       the push-config lifecycle and the SSRF guard (opt-in, --push-url)

``methods`` only proves a method is ROUTED (empty params, read -32601). That is a
much weaker claim than "it works" — so ``lifecycle`` and ``push`` drive the real
calls against the task the stream check created.

Exit codes: 0 all required checks passed · 1 a required check failed ·
2 could not reach the peer at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

# Windows consoles default to cp1252 and die on the glyphs below.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

CARD_PATH = "/.well-known/agent-card.json"

# The A2A 1.0 JSON-RPC method surface, and the v0.3 alias each replaced.
# `required` marks the four a peer must serve to be useful at all; the rest are
# optional capabilities whose absence is reported, not failed.
METHODS: list[tuple[str, str | None, bool]] = [
    ("SendMessage", "message/send", True),
    ("SendStreamingMessage", "message/stream", True),
    ("GetTask", "tasks/get", True),
    ("CancelTask", "tasks/cancel", True),
    ("ListTasks", None, False),
    ("SubscribeToTask", "tasks/resubscribe", False),
    ("CreateTaskPushNotificationConfig", "tasks/pushNotificationConfig/set", False),
    ("GetTaskPushNotificationConfig", "tasks/pushNotificationConfig/get", False),
    ("ListTaskPushNotificationConfigs", "tasks/pushNotificationConfig/list", False),
    ("DeleteTaskPushNotificationConfig", "tasks/pushNotificationConfig/delete", False),
    ("GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard", False),
]

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
VERSION_NOT_SUPPORTED = -32009

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_GLYPH = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "–"}


class Report:
    """Collects (section, check, status, detail) rows and renders them."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, section: str, check: str, status: str, detail: str = "") -> None:
        self.rows.append({"section": section, "check": check, "status": status, "detail": detail})

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["status"] == FAIL)

    def render(self) -> str:
        out: list[str] = []
        width = max((len(r["check"]) for r in self.rows), default=10)
        last = None
        for r in self.rows:
            if r["section"] != last:
                out.append(f"\n\033[1m{r['section']}\033[0m")
                last = r["section"]
            glyph = _GLYPH[r["status"]]
            out.append(f"  {glyph} {r['check']:<{width}}  {r['detail']}")
        counts = {s: sum(1 for r in self.rows if r["status"] == s) for s in (OK, WARN, FAIL, SKIP)}
        out.append(
            f"\n{counts[OK]} passed · {counts[WARN]} warnings · "
            f"{counts[FAIL]} failed · {counts[SKIP]} skipped"
        )
        return "\n".join(out)


# ── transport ────────────────────────────────────────────────────────────────


def _post(
    rpc_url: str, payload: dict, *, headers: dict[str, str], timeout: float
) -> tuple[int, dict | None, str]:
    """POST a JSON-RPC envelope. Returns (http_status, parsed_body, raw_text).

    A JSON-RPC *error* is a normal outcome here — we classify on the error code —
    so transport failures are the only thing that raises.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(rpc_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — operator-supplied URL
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    try:
        return status, json.loads(raw), raw
    except json.JSONDecodeError:
        return status, None, raw


def _get_json(url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — operator-supplied URL
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    try:
        return status, json.loads(raw), raw
    except json.JSONDecodeError:
        return status, None, raw


def _envelope(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def _message(text: str) -> dict:
    """A 1.0 message: ROLE_USER, untyped parts, contextId lives INSIDE the message."""
    return {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": str(uuid.uuid4())}


def _err_code(body: dict | None) -> int | None:
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    return err.get("code") if isinstance(err, dict) else None


# ── checks ───────────────────────────────────────────────────────────────────


def check_card(rep: Report, base: str, headers: dict, timeout: float) -> dict | None:
    """The card is the entry point: everything else is discovered from it."""
    url = base.rstrip("/") + CARD_PATH
    status, card, raw = _get_json(url, headers=headers, timeout=timeout)
    if status != 200 or not isinstance(card, dict):
        rep.add("card", "reachable", FAIL, f"HTTP {status} at {url} — {raw[:120]}")
        return None
    rep.add("card", "reachable", OK, url)

    for field in ("name", "description", "version"):
        if card.get(field):
            rep.add("card", field, OK, str(card[field])[:70])
        else:
            rep.add("card", field, FAIL, "missing")

    # 1.0 replaced 0.3's flat `url` with supportedInterfaces[]. Accept either,
    # but say which — a peer still on flat `url` is advertising 0.3 shape.
    ifaces = card.get("supportedInterfaces")
    if isinstance(ifaces, list) and ifaces:
        for i in ifaces:
            rep.add(
                "card",
                "interface",
                OK,
                f"{i.get('protocolBinding', '?')} v{i.get('protocolVersion', '?')} → {i.get('url', '?')}",
            )
    elif card.get("url"):
        rep.add("card", "interface", WARN, f"0.3-shape flat `url`: {card['url']} (no supportedInterfaces)")
    else:
        rep.add("card", "interface", FAIL, "no supportedInterfaces[] and no url")

    skills = card.get("skills") or []
    if skills:
        ids = ", ".join(str(s.get("id", "?")) for s in skills[:6])
        rep.add("card", "skills", OK, f"{len(skills)} — {ids}")
    else:
        rep.add("card", "skills", WARN, "none declared (peers can't route by skill)")

    caps = card.get("capabilities") or {}
    if caps.get("streaming"):
        rep.add("card", "streaming cap", OK, "declared")
    else:
        rep.add("card", "streaming cap", WARN, "not declared")
    if caps.get("pushNotifications"):
        rep.add("card", "push cap", OK, "declared")

    exts = [e.get("uri") for e in (caps.get("extensions") or []) if isinstance(e, dict)]
    if exts:
        rep.add("card", "extensions", OK, f"{len(exts)} declared")
        for u in exts:
            rep.add("card", f"  {str(u).rsplit('/', 1)[-1]}", OK, str(u))
    else:
        rep.add("card", "extensions", SKIP, "none declared")

    schemes = card.get("securitySchemes") or {}
    rep.add(
        "card",
        "securitySchemes",
        OK if schemes else SKIP,
        ", ".join(schemes) if schemes else "none (open, or enforced out-of-band)",
    )
    return card


def check_version(rep: Report, rpc: str, headers: dict, timeout: float) -> None:
    """Version negotiation, and the silent-failure mode it causes.

    The gate runs LAST in the dispatcher pipeline, which is the thing everyone
    gets wrong. In order: unknown method name → -32601; unparseable params →
    -32602; and only then is the ``A2A-Version`` header checked → -32009
    VERSION_NOT_SUPPORTED, where a *missing* header is read as "0.3".

    So the probe must send genuinely VALID params or it never reaches the gate —
    a malformed probe returns -32602 and looks like "the peer ignores the
    header". ``GetTask{id}`` for a nonexistent task is the cheapest valid call:
    it reaches the gate, and on success bottoms out at a harmless -32001.
    """
    payload = _envelope("GetTask", {"id": "a2a-conformance-probe-nonexistent"})

    _, body, _ = _post(rpc, payload, headers={**headers, "A2A-Version": "1.0"}, timeout=timeout)
    code = _err_code(body)
    if code == METHOD_NOT_FOUND:
        rep.add("version", "with 1.0 header", FAIL, "-32601 — peer does not serve 1.0 methods")
    elif code == VERSION_NOT_SUPPORTED:
        rep.add("version", "with 1.0 header", FAIL, "-32009 — peer rejects 1.0 (expects another version)")
    elif code == INVALID_PARAMS:
        rep.add("version", "with 1.0 header", WARN, "-32602 — probe never reached the version gate")
    else:
        rep.add("version", "with 1.0 header", OK, f"accepted (code={code}, -32001 = task not found)")

    _, body, _ = _post(rpc, payload, headers=headers, timeout=timeout)
    code = _err_code(body)
    if code == VERSION_NOT_SUPPORTED:
        rep.add(
            "version",
            "without header",
            OK,
            "-32009 — header is load-bearing; its absence is read as 0.3",
        )
    elif code == INVALID_PARAMS:
        rep.add("version", "without header", WARN, "-32602 — probe never reached the version gate")
    else:
        rep.add("version", "without header", WARN, f"peer tolerated a missing header (code={code})")

    _, body, _ = _post(rpc, payload, headers={**headers, "A2A-Version": "9.9"}, timeout=timeout)
    code = _err_code(body)
    rep.add(
        "version",
        "bogus version",
        OK if code == VERSION_NOT_SUPPORTED else WARN,
        "-32009 VERSION_NOT_SUPPORTED" if code == VERSION_NOT_SUPPORTED else f"code={code}",
    )


def check_methods(rep: Report, rpc: str, headers: dict, timeout: float) -> dict[str, bool]:
    """Probe each method with deliberately-empty params.

    -32601 means the method genuinely isn't mounted. Anything else — including
    -32602 invalid params — proves the method exists and got far enough to
    validate. This is the only way to enumerate a surface without side effects:
    empty params can't create a task, send a message, or delete a config.

    Param validation runs *before* the version gate, so this probe is
    header-independent by construction — it reports the mounted surface even
    against a peer that would reject our version.
    """
    h = {**headers, "A2A-Version": "1.0"}
    served: dict[str, bool] = {}
    for method, alias, required in METHODS:
        _, body, _ = _post(rpc, _envelope(method, {}), headers=h, timeout=timeout)
        code = _err_code(body)
        present = code != METHOD_NOT_FOUND
        served[method] = present
        if present:
            rep.add("methods", method, OK, "served" + (f"  (0.3: {alias})" if alias else ""))
        else:
            rep.add("methods", method, FAIL if required else SKIP, "not mounted (-32601)")
    return served


def check_v03_compat(rep: Report, rpc: str, headers: dict, timeout: float) -> None:
    """v0.3 aliases on the same endpoint let old clients keep working."""
    _, body, _ = _post(rpc, _envelope("message/send", {}), headers=headers, timeout=timeout)
    code = _err_code(body)
    if code == METHOD_NOT_FOUND:
        rep.add("compat", "v0.3 aliases", SKIP, "not mounted — 0.3 clients will break")
    else:
        rep.add("compat", "v0.3 aliases", OK, f"message/send answers (code={code})")


def check_stream(
    rep: Report, rpc: str, headers: dict, timeout: float, prompt: str, card: dict | None
) -> str:
    """Drive one real streaming turn and inspect the frames.

    This costs the peer a turn (tokens), so it is the one check that mutates
    anything — hence --no-turn. Returns the task id (or "") so the lifecycle
    checks below have a real task to operate on.
    """
    payload = _envelope("SendStreamingMessage", {"message": _message(prompt)})
    req = urllib.request.Request(rpc, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in {**headers, "A2A-Version": "1.0"}.items():
        req.add_header(k, v)

    frames: list[tuple[str, dict]] = []
    sse_event_names: set[str] = set()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — operator-supplied URL
            if r.status != 200:
                rep.add("stream", "SendStreamingMessage", FAIL, f"HTTP {r.status}")
                return
            for raw_line in r:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    sse_event_names.add(line[6:].strip())
                    continue
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob:
                    continue
                try:
                    data = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                result = data.get("result") or {}
                if not result:
                    if data.get("error"):
                        rep.add("stream", "frame error", FAIL, json.dumps(data["error"])[:140])
                    continue
                # 1.0: the frame type IS the single key of `result` (a proto oneof).
                kind = next(iter(result))
                frames.append((kind, result[kind] if isinstance(result[kind], dict) else {}))
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        rep.add("stream", "SendStreamingMessage", FAIL, f"transport: {e}")
        return ""

    if not frames:
        rep.add("stream", "SendStreamingMessage", FAIL, "no frames received")
        return ""
    rep.add("stream", "SendStreamingMessage", OK, f"{len(frames)} frames")

    kinds = [k for k, _ in frames]
    rep.add("stream", "frame kinds", OK, ", ".join(dict.fromkeys(kinds)))

    # The 0.3->1.0 trap: a `kind` field inside the payload means 0.3 shape.
    legacy = [k for k, p in frames if "kind" in p]
    rep.add(
        "stream",
        "oneof (not `kind`)",
        WARN if legacy else OK,
        "payloads carry a 0.3 `kind` field" if legacy else "1.0 oneof shape",
    )
    if sse_event_names - {"error"}:
        rep.add("stream", "SSE event names", WARN, f"named events: {sorted(sse_event_names)}")

    if "task" in kinds:
        rep.add("stream", "initial task frame", OK, "task snapshot sent first")
    else:
        rep.add("stream", "initial task frame", WARN, "no `task` frame — consumer has no task id")

    # The append trap: proto3 gives `append` no presence, so False serializes as
    # an ABSENT key. Absent/false = replace. A consumer that concatenates every
    # artifactUpdate doubles the answer.
    arts = [p for k, p in frames if k == "artifactUpdate"]
    if arts:
        appends = sum(1 for a in arts if a.get("append") is True)
        replaces = len(arts) - appends
        rep.add("stream", "artifactUpdate", OK, f"{appends} append · {replaces} replace")
        if replaces == 0:
            rep.add(
                "stream",
                "terminal replace",
                WARN,
                "no replace frame — a dropped delta would truncate the stored answer",
            )
        else:
            rep.add("stream", "terminal replace", OK, "authoritative full-text replace sent")
    else:
        rep.add("stream", "artifactUpdate", SKIP, "none (peer may reply with a `message` instead)")

    # Terminal state, in either 1.0 or 0.3 spelling.
    states = [
        p.get("status", {}).get("state")
        for k, p in frames
        if k == "statusUpdate" and isinstance(p.get("status"), dict)
    ]
    states = [s for s in states if s]
    terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "completed", "failed", "canceled"}
    hit = [s for s in states if s in terminal]
    if hit:
        rep.add("stream", "terminal state", OK, hit[-1])
    elif states:
        rep.add("stream", "terminal state", WARN, f"stream ended non-terminal (last: {states[-1]})")

    # Extensions: declared on the card vs actually observed on the wire.
    declared = {
        e.get("uri")
        for e in ((card or {}).get("capabilities", {}).get("extensions") or [])
        if isinstance(e, dict)
    }
    observed: set[str] = set()
    for _k, p in frames:
        for container in (p, p.get("artifact") or {}, p.get("status") or {}):
            if isinstance(container, dict):
                for key in (container.get("metadata") or {}):
                    if isinstance(key, str) and key.startswith("http"):
                        observed.add(key)
    if declared:
        for uri in sorted(declared):
            short = str(uri).rsplit("/", 1)[-1]
            if uri in observed:
                rep.add("ext", short, OK, "declared and emitted")
            else:
                rep.add("ext", short, SKIP, "declared; not emitted on this turn")
    for uri in sorted(observed - declared):
        rep.add("ext", str(uri).rsplit("/", 1)[-1], WARN, f"emitted but NOT declared on card: {uri}")
    if not declared and not observed:
        rep.add("ext", "extensions", SKIP, "none declared or observed")

    # Task id for the lifecycle checks — from the initial `task` frame, else any
    # frame that names one.
    for _k, p in frames:
        tid = p.get("id") if _k == "task" else p.get("taskId")
        if isinstance(tid, str) and tid:
            return tid
    return ""


def check_lifecycle(rep: Report, rpc: str, headers: dict, timeout: float, task_id: str) -> None:
    """Exercise the post-turn task methods against a REAL task.

    ``check_methods`` only proves a method is mounted (it probes with empty params
    and reads -32601). That is a much weaker claim than "it works": a method can
    be routed and still fail on every real call. These run the genuine article
    against the task the stream check just created.
    """
    h = {**headers, "A2A-Version": "1.0"}

    # GetTask — the task must still be retrievable after going terminal.
    _, body, _ = _post(rpc, _envelope("GetTask", {"id": task_id}), headers=h, timeout=timeout)
    code = _err_code(body)
    if code is None:
        task = ((body or {}).get("result") or {}).get("task") or (body or {}).get("result") or {}
        state = (task.get("status") or {}).get("state", "?")
        rep.add("lifecycle", "GetTask", OK, f"retrievable after terminal (state={state})")
    else:
        rep.add("lifecycle", "GetTask", FAIL, f"code={code}")

    # SubscribeToTask — the reconnect path. On an ALREADY-terminal task a peer may
    # legitimately answer with a final snapshot, an empty stream, or a "not
    # active" error; any of those beats -32601. We assert only that it is
    # implemented and doesn't blow up, because "reattach to a live task" can't be
    # tested without racing the turn.
    payload = _envelope("SubscribeToTask", {"id": task_id})
    req = urllib.request.Request(rpc, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in h.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, 15)) as r:  # noqa: S310 — operator-supplied URL
            first = ""
            for raw in r:
                first = raw.decode("utf-8", "replace").strip()
                if first.startswith("data:"):
                    break
            rep.add("lifecycle", "SubscribeToTask", OK, f"HTTP {r.status}" + (" + frames" if first else ""))
    except urllib.error.HTTPError as e:
        rep.add("lifecycle", "SubscribeToTask", WARN, f"HTTP {e.code} on a terminal task")
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        rep.add("lifecycle", "SubscribeToTask", WARN, f"{type(e).__name__}: {e}")


def check_push(rep: Report, rpc: str, headers: dict, timeout: float, task_id: str, callback: str) -> None:
    """Full push-notification config lifecycle: create → list → delete.

    Opt-in (--push-url) because registering a webhook on someone else's agent is a
    real side effect. We always attempt the delete, so a probe leaves no residue.

    In 1.0 ``TaskPushNotificationConfig`` is FLAT — ``{taskId, url, token}`` — not
    v0.3's ``{taskId, pushNotificationConfig: {...}}`` wrapper. Sending the 0.3
    shape here is a -32602, which is a good way to discover you are talking to a
    0.3 peer.
    """
    h = {**headers, "A2A-Version": "1.0"}
    params = {"taskId": task_id, "url": callback, "token": "a2a-conformance-probe"}

    _, body, _ = _post(rpc, _envelope("CreateTaskPushNotificationConfig", params), headers=h, timeout=timeout)
    code = _err_code(body)
    if code is not None:
        rep.add("push", "Create", FAIL, f"code={code} — {json.dumps((body or {}).get('error'))[:120]}")
        return
    cfg_id = (((body or {}).get("result") or {})).get("id", "")
    rep.add("push", "Create", OK, f"registered{f' (id={cfg_id})' if cfg_id else ''}")

    _, body, _ = _post(rpc, _envelope("ListTaskPushNotificationConfigs", {"taskId": task_id}), headers=h, timeout=timeout)
    if _err_code(body) is None:
        cfgs = ((body or {}).get("result") or {}).get("configs") or []
        rep.add("push", "List", OK, f"{len(cfgs)} config(s)")
    else:
        rep.add("push", "List", WARN, f"code={_err_code(body)}")

    # Always clean up — never leave a webhook registered on someone else's agent.
    # Delete keys on (taskId, id); `id` is the config id from Create, which some
    # peers omit — fall back to the task id, which the SDK store treats as the
    # default config id.
    del_params = {"taskId": task_id, "id": cfg_id or task_id}
    _, body, _ = _post(rpc, _envelope("DeleteTaskPushNotificationConfig", del_params), headers=h, timeout=timeout)
    if _err_code(body) is None:
        rep.add("push", "Delete", OK, "cleaned up")
    else:
        rep.add("push", "Delete", WARN, f"code={_err_code(body)} — config may persist on the peer")

    # SSRF guard. A push callback is an outbound request the AGENT makes, with a
    # shared secret attached — so an unguarded peer can be aimed at its own cloud
    # metadata endpoint or anything else on its network. Refusing this is the
    # correct behavior and the check PASSES on refusal; acceptance is the finding.
    unsafe = "http://169.254.169.254/latest/meta-data"
    _, body, _ = _post(
        rpc,
        _envelope("CreateTaskPushNotificationConfig", {"taskId": task_id, "url": unsafe}),
        headers=h,
        timeout=timeout,
    )
    if _err_code(body) is not None:
        rep.add("push", "SSRF guard", OK, "refused a link-local metadata callback")
    else:
        rep.add(
            "push",
            "SSRF guard",
            FAIL,
            f"peer ACCEPTED {unsafe} as a callback — it will POST task payloads there",
        )
        # We just armed something dangerous on the peer; remove it immediately.
        _post(
            rpc,
            _envelope("DeleteTaskPushNotificationConfig", {"taskId": task_id, "id": task_id}),
            headers=h,
            timeout=timeout,
        )


# ── main ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Probe an A2A 1.0 agent and report what it actually implements.",
        epilog="Exit 0 = all required checks passed, 1 = a required check failed, 2 = unreachable.",
    )
    ap.add_argument("--url", required=True, help="agent base URL (or its /a2a endpoint directly)")
    ap.add_argument("--token", default=None, help="bearer token, if the peer requires one")
    ap.add_argument("--api-key", default=None, help="X-API-Key, if the peer uses api-key auth")
    ap.add_argument("--timeout", type=float, default=60.0, help="per-request timeout (default 60s)")
    ap.add_argument(
        "--prompt",
        default="Reply with the single word: pong.",
        help="prompt for the one live turn the stream check drives",
    )
    ap.add_argument("--no-turn", action="store_true", help="skip the live turn (no tokens spent)")
    ap.add_argument(
        "--push-url",
        default=None,
        help="callback URL to exercise the push-notification config lifecycle "
        "(create/list/delete). Opt-in: registering a webhook on a peer is a real "
        "side effect. The probe always deletes what it created.",
    )
    ap.add_argument("--json", action="store_true", dest="as_json", help="emit the report as JSON")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    raw = args.url.rstrip("/")
    # Accept either the base URL or the /a2a endpoint; derive the other.
    base, rpc = (raw.rsplit("/a2a", 1)[0], raw) if raw.endswith("/a2a") else (raw, raw + "/a2a")

    headers: dict[str, str] = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    rep = Report()
    card = check_card(rep, base, headers, args.timeout)
    if card is None and not args.as_json:
        print(rep.render(), file=sys.stderr)
        print(f"\nCould not fetch the agent card at {base}{CARD_PATH} — is the peer up, and the token right?")
        return 2

    check_version(rep, rpc, headers, args.timeout)
    check_methods(rep, rpc, headers, args.timeout)
    check_v03_compat(rep, rpc, headers, args.timeout)
    if args.no_turn:
        rep.add("stream", "live turn", SKIP, "--no-turn")
        rep.add("lifecycle", "task methods", SKIP, "--no-turn (no task to operate on)")
    else:
        task_id = check_stream(rep, rpc, headers, args.timeout, args.prompt, card)
        if task_id:
            check_lifecycle(rep, rpc, headers, args.timeout, task_id)
            if args.push_url:
                check_push(rep, rpc, headers, args.timeout, task_id, args.push_url)
            else:
                rep.add("push", "config lifecycle", SKIP, "pass --push-url <callback> to exercise")
        else:
            rep.add("lifecycle", "task methods", SKIP, "the turn produced no task id")

    if args.as_json:
        print(json.dumps({"url": base, "rpc": rpc, "checks": rep.rows}, indent=2))
    else:
        print(rep.render())
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
