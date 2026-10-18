"""``protoagent pair`` — let another agent (or, with ``--device``, a phone) pair with the
protoAgent running on this machine.

ADR 0113 D7. The remote side of agent pairing usually happens in the console (Settings ▸
Devices ▸ Pair an agent), but a docker or server install has nobody at its console. This
asks the running instance for a short agent pairing code and prints it with the addresses a
hub can reach it on, so the hub side (``protoagent fleet pair <url> <code>``, or the Fleet
panel's "Pair…") can claim it. The hub never sees this instance's shared bearer: the claim
mints a per-hub device token that can be revoked here on its own (ADR 0087 D1).

``--device`` is the same for a PHONE (ADR 0087 D3 as amended 2026-10-18): it starts a device
pairing and prints the short code, the console URL(s), and a terminal QR of the best one — so
a headless box with nobody at its console can still pair a phone. The phone scans the QR, or
opens the URL and types the code into "Have a pairing code?".

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
            "running on this machine (ADR 0113). The code works once and expires in minutes. "
            "With --device, pair a phone or tablet instead (ADR 0087)."
        ),
    )
    p.add_argument(
        "--device",
        action="store_true",
        help="pair a phone/tablet: print its code, the URL to open, and a terminal QR",
    )
    p.add_argument("--no-qr", action="store_true", help="with --device: skip the terminal QR")
    p.add_argument(
        "--light",
        action="store_true",
        help="with --device: draw the QR for a light-background terminal (default assumes dark)",
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


def _terminal_qr(url: str, *, light: bool) -> str | None:
    """``url`` as a QR drawn in half-block characters, or None if it can't be rendered.

    ``invert`` is about the TERMINAL, not the code: a scanner wants dark modules on a light
    ground, and on a dark terminal the "light" modules are the printed blocks. Never fatal —
    the code and URL printed beside it are the fallback, exactly like the console's QR.
    """
    try:
        import io

        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=not light)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — an optional visual must never break pairing
        return None


def _write(text: str) -> None:
    """Print, but survive a console that can't encode the QR's block characters (a legacy
    Windows code page) — the code and URL lines matter, the drawing doesn't."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        print("(this terminal can't draw the QR — open the URL below and type the code)")


def _host_note(h: dict) -> str:
    """What a phone needs to use this address — said plainly (#4336 S4)."""
    kind = h.get("kind") or ""
    if kind == "tailnet":
        need = "the phone needs the Tailscale app, signed in to the same tailnet"
        if h.get("funnel"):
            return "HTTPS, also PUBLIC through Tailscale Funnel"
        if h.get("secure"):
            return f"HTTPS from any network; {need}"
        return f"any network; {need}"
    if kind == "lan":
        return "same Wi-Fi only"
    return kind


def _print_device_code(res: dict, *, qr: bool, light: bool) -> None:
    code = res.get("code") or ""
    left = _remaining(res.get("expires_at"))
    hosts = res.get("hosts") or []
    best = hosts[0] if hosts else {}
    if qr and best.get("url"):
        drawn = _terminal_qr(str(best["url"]), light=light)
        if drawn:
            print("Scan with the phone's camera:")
            _write(drawn)
            print()
    print(f"Pairing code:  {code}" + (f"   (expires in {left})" if left else ""))
    print()
    print('Or on the phone, open this agent and tap "Have a pairing code?", then type the code:')
    for h in hosts:
        base = h.get("base_url") or str(h.get("url") or "").split("#", 1)[0]
        note = _host_note(h)
        print(f"  {base}" + (f"   ({note})" if note else ""))
    print()
    print("The code works once. The phone shows up under Settings ▸ Devices, where you can revoke it.")


def _print_unreachable(res: dict, again: str = "protoagent pair") -> None:
    print(res.get("error") or "This agent only listens on localhost, so no other agent can reach it.", file=sys.stderr)
    avail = res.get("available") or []
    if avail:
        where = ", ".join(f"{a.get('host')} ({a.get('kind')})" for a in avail)
        print(f"It could be reached on: {where}", file=sys.stderr)
    # Never suggest PROTOAGENT_ALLOW_OPEN (ADR 0087 D6): the fix is a reachable bind WITH a
    # token, and the server refuses a non-loopback bind without one.
    ts = ((res.get("reach") or {}).get("tailscale") or {}) if isinstance(res.get("reach"), dict) else {}
    if ts.get("running") and ts.get("dns_name"):
        # The localhost-friendly path (ADR 0121 D1): tailscale serve proxies HTTPS to this
        # instance's loopback port, so nothing has to listen on the network at all.
        print(
            f"This machine is on Tailscale ({ts['dns_name']}). The simplest fix: Settings ▸ Devices ▸ "
            "'Use HTTPS via Tailscale' (it needs an auth token, and makes one if there isn't one).",
            file=sys.stderr,
        )
    print(
        "Or listen on one network interface with an auth token configured — Settings ▸ Devices "
        "offers the addresses and does both — or start it with `--host <that address>` (it refuses "
        f"to start without a token). Then run `{again}` again.",
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
        res = conn.client.pairing_start("device" if args.device else "agent")
    except deckhub.HubError as exc:
        print(f"protoagent pair: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.client.close()

    if args.device:
        if args.json:
            print(json.dumps(res, indent=2))
            return 0 if res.get("ok") else 1
        if not res.get("ok"):
            if res.get("reason") == "no_console":
                # `--ui none`: nothing for the phone to open. The bind advice below would be
                # beside the point — say the one thing that fixes it.
                print(f"protoagent pair: {res.get('error')}", file=sys.stderr)
                return 1
            _print_unreachable(res, again="protoagent pair --device")
            return 1
        _print_device_code(res, qr=not args.no_qr, light=args.light)
        return 0
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
