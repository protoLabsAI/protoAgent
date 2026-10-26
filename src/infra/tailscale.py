"""Tailscale discovery and ``tailscale serve`` control for device pairing (#4336 S4, ADR 0121 D1).

A phone with Tailscale reaches this instance best at ``https://<machine>.<tailnet>.ts.net``:
a stable name (no IP to go stale), a real certificate (a secure context, so the console can
use the APIs that need one), and the instance can stay bound to localhost because
``tailscaled`` terminates TLS and proxies to ``127.0.0.1:<port>``. ADR 0121 D1 makes that the
preferred home origin for a phone.

Everything here shells out to the ``tailscale`` CLI. Nothing is required: an absent CLI, a
stopped daemon, a slow answer or unparseable JSON all degrade to "Tailscale isn't available"
and pairing falls back to the IP addresses it always offered.

- :func:`status` reads ``tailscale status --json`` (bounded, cached).
- :func:`served_url` reads ``tailscale serve status --json`` and answers "does an HTTPS
  handler at ``/`` already proxy to THIS instance's port?".
- :func:`enable_serve` / :func:`disable_serve` change the serve config, but only ever the
  ``/`` handler of an HTTPS port that proxies to this instance. Never ``tailscale serve
  reset``: other serve config on the machine belongs to someone else.

The security rule that goes with ``enable_serve`` (a token must be configured first, because
serve's requests arrive from 127.0.0.1) is enforced by the caller,
``operator_api.pairing_routes``, and by ``a2a_impl.hosts`` for an instance that later drops
its token. See ``docs/guides/phone-access.md``.

Neutral like the rest of ``infra``: stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger("protoagent.infra.tailscale")

# Where to turn on HTTPS certificates for a tailnet. `tailscale serve` needs them, and a
# tailnet admin has to enable them once.
HTTPS_ADMIN_URL = "https://login.tailscale.com/admin/dns"

# Every CLI call is bounded. `status` answers in milliseconds when the daemon is up; the cap
# is for a wedged daemon. `serve` can BLOCK waiting for the operator to approve Serve in a
# browser (newer CLIs print a consent URL and poll), so it gets longer but is still capped.
STATUS_TIMEOUT = 3.0
SERVE_TIMEOUT = 20.0

# Discovery is cached briefly: the Devices panel asks on every "Add a device", and tailnet
# state changes on the order of minutes.
_CACHE_TTL = 15.0

# HTTPS ports `enable_serve` will use, in order. 443 gives the clean `https://<name>` URL;
# the others let a second instance on the same machine (the dev sandbox on :7871) get its own
# handler instead of overwriting the first one's.
SERVE_HTTPS_PORTS = (443, 8443, 10000)

_LOOPBACK_TARGET_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


@dataclass
class TailscaleStatus:
    """What `tailscale status --json` says about this machine."""

    available: bool = False  # the CLI exists and answered with JSON
    running: bool = False  # BackendState == "Running" (logged in, connected)
    dns_name: str = ""  # Self.DNSName without the trailing dot, e.g. "box.tail1234.ts.net"
    magicdns: bool = False  # MagicDNS is on for the tailnet, so dns_name resolves for peers
    https: bool = False  # HTTPS certificates are enabled for the tailnet
    ips: list[str] = field(default_factory=list)  # Self.TailscaleIPs
    error: str = ""  # why available/running is False, for the panel

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "running": self.running,
            "dns_name": self.dns_name,
            "magicdns": self.magicdns,
            "https": self.https,
            "ips": list(self.ips),
            "error": self.error,
        }


@dataclass
class ServeResult:
    ok: bool
    reason: str = ""  # machine-readable: no_cli, not_running, https_disabled, permission, ports_busy, failed
    error: str = ""  # human-readable
    url: str = ""  # the https URL now serving this instance
    enable_url: str = ""  # where the operator turns on what's missing

    def as_dict(self) -> dict:
        out: dict = {"ok": self.ok}
        for key in ("reason", "error", "url", "enable_url"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


# ── the CLI seam (tests replace `_run`) ─────────────────────────────────────────────────────

# The macOS app ships its CLI inside the bundle and only puts it on PATH if the user asks.
_MAC_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def cli_path() -> str | None:
    """The ``tailscale`` executable, or None. ``PROTOAGENT_TAILSCALE_BIN`` overrides (and an
    empty value disables discovery entirely)."""
    override = os.environ.get("PROTOAGENT_TAILSCALE_BIN")
    if override is not None:
        return override.strip() or None
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "darwin" and os.path.exists(_MAC_APP_CLI):
        return _MAC_APP_CLI
    return None


def _cli_env(exe: str) -> dict[str, str] | None:
    """The macOS app's bundled binary is the GUI too: with no ``TERM`` (a desktop app
    launched from the Dock or Finder) it tries to start the GUI and fails with "The
    Tailscale GUI failed to start" instead of answering. ``TAILSCALE_BE_CLI=1`` forces
    CLI mode. Other installs get the inherited environment unchanged."""
    if os.path.basename(exe) != "Tailscale":
        return None
    return {**os.environ, "TAILSCALE_BE_CLI": "1"}


def _run(args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run the tailscale CLI → ``(returncode, stdout, stderr)``.

    ``-1`` means it couldn't run at all (missing binary, OS error); ``-2`` means it timed
    out, with whatever it printed before that (``tailscale serve`` prints a consent URL and
    then waits for it to be approved)."""
    exe = cli_path()
    if not exe:
        return -1, "", "tailscale CLI not found"
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_cli_env(exe),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return -2, out, err
    except OSError as exc:
        return -1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ── status ──────────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(key: str, fn):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def parse_status(raw: str) -> TailscaleStatus:
    """``tailscale status --json`` → :class:`TailscaleStatus`. Pure."""
    try:
        data = json.loads(raw)
    except ValueError:
        return TailscaleStatus(error="tailscale status returned no JSON")
    if not isinstance(data, dict):
        return TailscaleStatus(error="tailscale status returned no JSON")
    st = TailscaleStatus(available=True)
    backend = str(data.get("BackendState") or "")
    st.running = backend == "Running"
    if not st.running:
        st.error = f"Tailscale is not connected ({backend or 'unknown state'})"
    me = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    st.dns_name = str(me.get("DNSName") or "").strip().rstrip(".").lower()
    st.ips = [str(ip) for ip in (me.get("TailscaleIPs") or []) if isinstance(ip, str)]
    tailnet = data.get("CurrentTailnet") if isinstance(data.get("CurrentTailnet"), dict) else {}
    # Older daemons have no CurrentTailnet; a MagicDNS suffix on the status means it's on.
    st.magicdns = bool(tailnet.get("MagicDNSEnabled", bool(data.get("MagicDNSSuffix"))))
    # HTTPS certs are enabled when the daemon lists cert domains, or the node carries the
    # `https` capability (newer daemons put it in CapMap, older ones in Capabilities).
    caps = set(me.get("Capabilities") or []) | set((me.get("CapMap") or {}).keys())
    st.https = bool(data.get("CertDomains")) or "https" in caps
    return st


def status(*, fresh: bool = False) -> TailscaleStatus:
    """This machine's Tailscale state. Never raises; cached for a few seconds."""

    def _probe() -> TailscaleStatus:
        code, out, err = _run(["status", "--json"], STATUS_TIMEOUT)
        if code == -1:
            return TailscaleStatus(error="Tailscale isn't installed on this machine")
        if code == -2:
            return TailscaleStatus(error="tailscale status timed out")
        # `status --json` exits non-zero when stopped but still prints JSON — parse first.
        st = parse_status(out)
        if not st.available and err.strip():
            st.error = err.strip().splitlines()[-1][:200]
        return st

    if fresh:
        clear_cache()
    return _cached("status", _probe)


# ── serve ───────────────────────────────────────────────────────────────────────────────────


def _proxies_to(target: str, port: int) -> bool:
    """Does a serve handler's ``Proxy`` value point at this instance on loopback?
    Accepts the forms the CLI writes: ``http://127.0.0.1:7870``, ``http://localhost:7870/``,
    ``127.0.0.1:7870``."""
    t = (target or "").strip()
    if not t:
        return False
    if "://" not in t:
        t = "http://" + t
    try:
        parts = urlsplit(t)
        tport = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or tport != port:
        return False
    if (parts.path or "/") != "/":
        return False
    return (parts.hostname or "").lower() in _LOOPBACK_TARGET_HOSTS


def parse_serve(raw: str, port: int) -> dict:
    """``tailscale serve status --json`` → what it says about THIS instance. Pure.

    Returns ``{"ours": {https_port: host}, "taken": {https_port, …}, "funnel": {https_port, …}}``:
    the HTTPS ports whose ``/`` handler proxies to us (with the name it's served under),
    every HTTPS port already used by anything, and which of ours are also exposed to the
    public internet through Funnel."""
    ours: dict[int, str] = {}
    taken: set[int] = set()
    funnel: set[int] = set()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for p in (data.get("TCP") or {}).keys():
        try:
            taken.add(int(p))
        except (TypeError, ValueError):
            continue
    for hostport, web in (data.get("Web") or {}).items():
        host, _, p = str(hostport).rpartition(":")
        try:
            https_port = int(p)
        except ValueError:
            continue
        taken.add(https_port)
        handler = ((web or {}).get("Handlers") or {}).get("/") or {}
        if _proxies_to(str(handler.get("Proxy") or ""), port):
            ours[https_port] = host.lower()
    for hostport, on in (data.get("AllowFunnel") or {}).items():
        _, _, p = str(hostport).rpartition(":")
        if on and p.isdigit() and int(p) in ours:
            funnel.add(int(p))
    return {"ours": ours, "taken": taken, "funnel": funnel}


def https_url(host: str, https_port: int) -> str:
    return f"https://{host}" if https_port == 443 else f"https://{host}:{https_port}"


def _serve_state(port: int) -> dict | None:
    code, out, _err = _run(["serve", "status", "--json"], STATUS_TIMEOUT)
    if code != 0:
        return None
    return parse_serve(out, port)


def served_url(port: int, *, fresh: bool = False) -> dict | None:
    """``{"url", "https_port", "funnel"}`` when ``tailscale serve`` already proxies HTTPS to
    ``127.0.0.1:<port>``, else None. Never raises; cached briefly."""

    def _probe() -> dict | None:
        st = status()
        if not (st.available and st.running):
            return None
        state = _serve_state(port)
        if not state or not state["ours"]:
            return None
        https_port = min(state["ours"])  # 443 first when there are several
        host = state["ours"][https_port] or st.dns_name
        return {"url": https_url(host, https_port), "https_port": https_port, "funnel": https_port in state["funnel"]}

    if fresh:
        with _lock:
            _cache.pop(f"serve:{port}", None)
    return _cached(f"serve:{port}", _probe)


_CONSENT_URL = re.compile(r"https://login\.tailscale\.com/\S+")


def _classify_failure(out: str, err: str) -> ServeResult:
    text = f"{out}\n{err}"
    lower = text.lower()
    consent = _CONSENT_URL.search(text)
    if consent or "not enabled" in lower:
        return ServeResult(
            ok=False,
            reason="https_disabled",
            error="Tailscale Serve and HTTPS certificates aren't enabled on this tailnet yet. Enable them, then try again.",
            enable_url=consent.group(0).rstrip(".,") if consent else HTTPS_ADMIN_URL,
        )
    if "access denied" in lower or "permission" in lower or "operator" in lower:
        return ServeResult(
            ok=False,
            reason="permission",
            error=(
                "Tailscale refused to change its serve config for this user. On Linux, run "
                "`sudo tailscale set --operator=$USER` once, then try again."
            ),
        )
    last = (err.strip() or out.strip()).splitlines()
    return ServeResult(ok=False, reason="failed", error=(last[-1] if last else "tailscale serve failed")[:300])


def enable_serve(port: int) -> ServeResult:
    """Serve ``http://127.0.0.1:<port>`` over HTTPS on the tailnet, in the background.

    Idempotent: already served → that URL. Picks the first HTTPS port in
    :data:`SERVE_HTTPS_PORTS` nothing else uses, so it never overwrites another service's
    handler. The CALLER must have checked that this instance requires a token."""
    st = status(fresh=True)
    if not st.available:
        return ServeResult(ok=False, reason="no_cli", error=st.error or "Tailscale isn't installed on this machine")
    if not st.running:
        return ServeResult(ok=False, reason="not_running", error=st.error or "Tailscale is not connected")
    if not st.https:
        # Checked up front: on a tailnet without HTTPS, newer CLIs print a consent URL and
        # then BLOCK until someone approves it in a browser.
        return ServeResult(
            ok=False,
            reason="https_disabled",
            error="HTTPS certificates aren't enabled on this tailnet. Enable them in the Tailscale admin console (DNS ▸ HTTPS Certificates), then try again.",
            enable_url=HTTPS_ADMIN_URL,
        )
    state = _serve_state(port)
    if state is None:
        return ServeResult(ok=False, reason="failed", error="couldn't read the current tailscale serve config")
    if state["ours"]:
        https_port = min(state["ours"])
        return ServeResult(ok=True, url=https_url(state["ours"][https_port] or st.dns_name, https_port))
    free = [p for p in SERVE_HTTPS_PORTS if p not in state["taken"]]
    if not free:
        return ServeResult(
            ok=False,
            reason="ports_busy",
            error=f"HTTPS ports {', '.join(map(str, SERVE_HTTPS_PORTS))} are already served by something else on this machine.",
        )
    https_port = free[0]
    code, out, err = _run(["serve", "--bg", f"--https={https_port}", f"http://127.0.0.1:{port}"], SERVE_TIMEOUT)
    with _lock:
        _cache.pop(f"serve:{port}", None)
    if code != 0:
        return _classify_failure(out, err)
    log.info("[tailscale] serving http://127.0.0.1:%d at https port %d", port, https_port)
    return ServeResult(ok=True, url=https_url(st.dns_name, https_port))


def disable_serve(port: int) -> ServeResult:
    """Turn off the HTTPS handler(s) that proxy to THIS instance — and nothing else."""
    st = status(fresh=True)
    if not (st.available and st.running):
        return ServeResult(ok=False, reason="not_running", error=st.error or "Tailscale is not connected")
    state = _serve_state(port)
    if state is None:
        return ServeResult(ok=False, reason="failed", error="couldn't read the current tailscale serve config")
    for https_port in sorted(state["ours"]):
        code, out, err = _run(["serve", f"--https={https_port}", "off"], SERVE_TIMEOUT)
        if code != 0:
            with _lock:
                _cache.pop(f"serve:{port}", None)
            return _classify_failure(out, err)
        log.info("[tailscale] stopped serving http://127.0.0.1:%d on https port %d", port, https_port)
    with _lock:
        _cache.pop(f"serve:{port}", None)
    return ServeResult(ok=True)
