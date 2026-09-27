"""``protoagent pair`` — let another agent pair with the protoAgent running on this machine.

ADR 0113 D7. The remote side of agent pairing usually happens in the console (Settings ▸
Devices ▸ Pair an agent), but a docker or server install has nobody at its console. This
asks the running instance for a short agent pairing code and prints it with the addresses a
hub can reach it on, so the hub side (``protoagent fleet pair <url> <code>``, or the Fleet
panel's "Pair…") can claim it. The hub never sees this instance's shared bearer: the claim
mints a per-hub device token that can be revoked here on its own (ADR 0087 D1).

Finding and opening the local instance is the fleet deck's job (``deck.hub.connect``): the
pidfile / heartbeats under every known box root, then the fleet service token read from disk
for loopback only. Nothing here reads, prints, or logs a credential.

Neutral by contract like the rest of ``deck/``: httpx + ``infra`` only, never ``server``,
``operator_api`` or ``graph``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from deck import hub as deckhub


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protoagent pair",
        description=(
            "Print a one-time code another agent's hub can use to pair with the protoAgent "
            "running on this machine (ADR 0113). The code works once and expires in minutes."
        ),
    )
    p.add_argument("--url", default=None, help="the instance to pair (default: the one running on this machine)")
    p.add_argument("--token", default=None, help=f"operator credential for --url (default: ${deckhub.ENV_TOKEN} / this box's)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--insecure-http",
        action="store_true",
        help="allow sending a credential over plain http to a non-loopback --url",
    )
    return p


def _remaining(expires_at: Any) -> str:
    try:
        secs = max(0, int(float(expires_at) - time.time()))
    except (TypeError, ValueError):
        return ""
    return f"{secs // 60}:{secs % 60:02d}"


def _host_url(h: dict, port: int | None) -> str:
    if h.get("url"):
        return str(h["url"])
    return f"http://{h.get('host')}:{port}" if port else f"http://{h.get('host')}"


def _print_code(res: dict, port: int | None) -> None:
    name = res.get("name") or "this agent"
    code = res.get("code") or ""
    left = _remaining(res.get("expires_at"))
    print(f"Pairing code for {name}:  {code}" + (f"   (expires in {left})" if left else ""))
    print()
    print("On the hub, enter it under Settings ▸ Agents ▸ Pair…, or run:")
    for h in res.get("hosts") or []:
        kind = h.get("kind") or ""
        print(f"  protoagent fleet pair {_host_url(h, port)} {code}" + (f"   ({kind})" if kind else ""))
    print()
    print("The code works once. The hub it pairs shows up under Settings ▸ Devices, where you can revoke it.")


def _print_unreachable(res: dict) -> None:
    print(res.get("error") or "This agent only listens on localhost, so no other agent can reach it.", file=sys.stderr)
    avail = res.get("available") or []
    if avail:
        where = ", ".join(f"{a.get('host')} ({a.get('kind')})" for a in avail)
        print(f"It could be reached on: {where}", file=sys.stderr)
    # Never suggest PROTOAGENT_ALLOW_OPEN (ADR 0087 D6): the fix is a reachable bind WITH a
    # token, and the server refuses a non-loopback bind without one.
    print(
        "To make it pairable, bind every interface with an auth token configured — Settings ▸ "
        "Devices ▸ 'Allow devices on my network' does both — or start it with `--host 0.0.0.0` "
        "(it refuses to start that way without a token). Then run `protoagent pair` again.",
        file=sys.stderr,
    )
    if res.get("auth_configured") is False:
        print("This instance has no auth token yet; the Devices flow mints one.", file=sys.stderr)


def run_pair_cli(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    # `python -m server` configures INFO logging at import, and httpx would narrate every
    # probe around the code the operator is here to read (same as the fleet CLI).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        conn = deckhub.connect(url=args.url, token=args.token, insecure_http=args.insecure_http)
    except deckhub.NoHub as exc:
        if exc.members and not (exc.unauthorized or exc.failed):
            print(
                "protoagent pair: only a fleet MEMBER answered — members listen on loopback and "
                "are reached through their hub. Pair the hub instead.",
                file=sys.stderr,
            )
        else:
            print(f"protoagent pair: {exc}", file=sys.stderr)
        return 1
    try:
        res = conn.client.pairing_start("agent")
    except deckhub.HubError as exc:
        print(f"protoagent pair: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.client.close()

    if res.get("ok") and res.get("kind") != "agent":
        # An instance from before agent pairing ignores `kind` and mints a PHONE code, whose
        # url is a `#pair=` console link a hub cannot claim with. Say so rather than print it.
        print(
            "protoagent pair: this instance predates agent pairing (ADR 0113) and issued a phone "
            "code instead — update it, then run `protoagent pair` again.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        _print_unreachable(res)
        return 1
    # Hosts carry their own url; the port we reached the instance on is only the fallback.
    _print_code(res, urlsplit(conn.client.url).port)
    return 0
