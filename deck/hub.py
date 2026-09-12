"""Hub client for the fleet deck (#3467, epic #3466) — how a terminal talks to a RUNNING hub.

Why a client and not the supervisor: ``protoagent fleet ls`` used to read ``fleet.json``
from whatever instance root the shell resolved and fabricate a "running" host row from its
own pid. Beside the desktop hub — which runs under ``PROTOAGENT_HOME=~/Library/Application
Support/studio.protolabs.protoagent`` — it reported a fleet of one while twelve workspaces
were registered and five were online. The hub's ``GET /api/fleet`` (ADR 0042) is the only
source of live truth, and in live mode every mutation must go through the hub so the hub
keeps owning its child processes: a ``supervisor.up`` next to a running hub spawns members
the hub cannot see (``_is_our_agent``). This resolves ADR 0075's open question 2 the lean
way: live hub first, badged disk fallback.

Three jobs, in order:

1. **Find a hub.** ``discover_hubs()`` lists candidates by *evidence*, never by the
   environment the shell inherited: this instance's ``server.pid`` (``protoagent up``),
   the ``.instances/<pid>.json`` heartbeats every server writes under its box root
   (``infra.paths.register_instance``) — scanned across every box root this machine is
   known to use, including the desktop app's — and finally the default port.
2. **Open it.** ``token_chain()`` yields credentials to try: ``--token`` / the env, then the
   hub's own fleet service token (``<instance root>/workspaces/.fleet-token``, ADR 0089 —
   operator tier on the hub and on every member through the proxy), then the operator
   bearer env, then no credential (open mode). ``connect()`` tries them until the roster
   reads. Tokens are read from disk and never logged, printed, or echoed in errors.
3. **Talk.** ``HubClient`` wraps the handful of routes the CLI needs with bounded timeouts
   and typed errors. A ``401`` on ``/agents/<slug>/…`` is that MEMBER's credential problem
   (``MemberUnauthorized``), never the hub's — the July 2026 remote audit (#1607/#1609)
   found the console conflating the two, and the deck must not repeat it.

Neutral by contract: httpx + ``infra`` only. Never imports ``server``, ``operator_api``,
or ``graph`` (the fleet CLI in ``graph/`` imports *this*).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from infra.paths import box_root, data_home, instance_paths, pid_alive

DEFAULT_PORT = 7870
ENV_TOKEN = "PROTOAGENT_HUB_TOKEN"
# The operator bearer env the auth middleware reads at boot (a2a_impl/auth.py).
ENV_OPERATOR_BEARER = "A2A_AUTH_TOKEN"
# Same file the hub mints (graph/fleet/service_token.py): <workspaces root>/.fleet-token.
FLEET_TOKEN_FILE = ".fleet-token"
# The Tauri identifier — the desktop app sets PROTOAGENT_HOME = PROTOAGENT_BOX_ROOT = its
# per-user app-data dir for this id (apps/desktop/src-tauri/src/lib.rs), so a shell that
# wants the desktop hub's heartbeats and fleet token has to look there.
DESKTOP_APP_ID = "studio.protolabs.protoagent"

_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_PROBE_TIMEOUT = httpx.Timeout(2.0, connect=1.0)


# ── errors ────────────────────────────────────────────────────────────────────


class HubError(RuntimeError):
    """Base for every hub-client failure; ``url`` names the hub involved."""

    def __init__(self, url: str, message: str):
        super().__init__(message)
        self.url = url


class HubUnreachable(HubError):
    """No TCP/HTTP answer from the hub within the bounded timeout."""


class HubUnauthorized(HubError):
    """The HUB rejected our credential (a 401 on a hub path)."""


class MemberUnauthorized(HubError):
    """A MEMBER behind the proxy rejected the credential the hub attached (a 401 on
    ``/agents/<slug>/…``). This is the member's problem, not the hub's — surface it per
    member, never as a hub auth failure."""

    def __init__(self, url: str, slug: str, message: str):
        super().__init__(url, message)
        self.slug = slug


class HubRequestError(HubError):
    """The hub answered with a non-2xx it meant (400 / 409 / 5xx) — ``detail`` is its body."""

    def __init__(self, url: str, status: int, detail: str):
        super().__init__(url, f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class NoHub(HubError):
    """No candidate hub could be opened. ``tried`` / ``unauthorized`` / ``members`` summarize
    the search so the CLI can say exactly what it looked at."""

    def __init__(self, tried: list[str], unauthorized: list[str], members: list[str] | None = None):
        self.tried = tried
        self.unauthorized = unauthorized
        self.members = list(members or [])
        if unauthorized:
            msg = (
                f"a hub answered at {', '.join(unauthorized)} but rejected every credential — "
                f"pass --token or set {ENV_TOKEN}"
            )
        elif self.members and len(self.members) == len(tried):
            msg = f"only fleet MEMBERS answered ({', '.join(self.members)}) — their hub is not running"
        elif tried:
            msg = f"no hub answered at {', '.join(tried)}"
        else:
            msg = "no hub candidates on this box"
        super().__init__(unauthorized[0] if unauthorized else (tried[0] if tried else ""), msg)


# ── candidates ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HubCandidate:
    """A place a hub might be, and the evidence for it."""

    url: str
    source: str  # "flag" | "pidfile" | "heartbeat" | "default"
    instance_root: Path | None = None
    identity: str = ""
    pid: int | None = None


# The spawn-time marker the supervisor writes into a MEMBER's instance root
# (graph/workspaces/manager.py::is_workspace_member). A member is a full server — it
# writes a heartbeat and serves /api/fleet like a hub — so discovery has to tell the two
# apart by evidence or `fleet down <name>` lands on a member that has no such member.
WORKSPACE_MARKER = "workspace.yaml"


def is_member_root(instance_root: Path | None) -> bool:
    """True when ``instance_root`` is a fleet MEMBER's (spawned by some hub's supervisor)."""
    if instance_root is None:
        return False
    try:
        return (instance_root / WORKSPACE_MARKER).is_file()
    except OSError:
        return False


def normalize_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if not u:
        raise ValueError("hub url is empty")
    if "://" not in u:
        u = f"http://{u}"
    return u


def _loopback(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def desktop_box_roots() -> list[Path]:
    """Where the desktop app keeps its box root on each platform (Tauri ``app_data_dir``
    for :data:`DESKTOP_APP_ID`). Existence is checked by the caller."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / DESKTOP_APP_ID]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return [base / DESKTOP_APP_ID]
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else home / ".local" / "share"
    return [base / DESKTOP_APP_ID]


def known_box_roots() -> list[Path]:
    """Every box root this machine is known to use, existing dirs only, deduped in
    priority order: this shell's resolved box root, the plain data home, the desktop's."""
    seen: list[Path] = []
    for p in [box_root(), data_home(), *desktop_box_roots()]:
        try:
            rp = p.expanduser().resolve()
        except OSError:
            continue
        if rp in seen or not rp.is_dir():
            continue
        seen.append(rp)
    return seen


def read_heartbeats(root: Path) -> list[dict]:
    """The live ``.instances/<pid>.json`` records under ``root``. Dead pids are SKIPPED,
    never unlinked — pruning is the owning server's job (``infra.paths.colocated_instances``);
    a read-only CLI must not mutate another box root's state."""
    d = root / ".instances"
    out: list[dict] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        if pid == os.getpid() or not pid_alive(pid):
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = {}
        if not isinstance(rec, dict):
            rec = {}
        out.append(
            {
                "pid": pid,
                "port": rec.get("port"),
                "identity": str(rec.get("identity") or ""),
                "instance_root": str(rec.get("instance_root") or ""),
            }
        )
    return out


def _pidfile_candidate() -> HubCandidate | None:
    """The server ``protoagent up`` started for THIS instance (``<instance root>/server.pid``)."""
    root = instance_paths().instance_root
    p = root / "server.pid"
    try:
        rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    try:
        pid = int(rec.get("pid") or 0)
        port = int(rec.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if port <= 0 or not pid_alive(pid):
        return None
    return HubCandidate(_loopback(port), "pidfile", instance_root=root, pid=pid)


def discover_hubs(*, explicit_url: str | None = None) -> list[HubCandidate]:
    """Candidate hubs in priority order, deduped by URL. ``explicit_url`` short-circuits
    discovery entirely (the operator said where the hub is)."""
    if explicit_url:
        return [HubCandidate(normalize_url(explicit_url), "flag")]
    out: list[HubCandidate] = []
    seen: set[str] = set()

    def add(c: HubCandidate) -> None:
        if c.url not in seen:
            seen.add(c.url)
            out.append(c)

    if (own := _pidfile_candidate()) is not None:
        add(own)
    for root in known_box_roots():
        for hb in read_heartbeats(root):
            try:
                port = int(hb.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if port <= 0:
                continue
            iroot = Path(hb["instance_root"]) if hb.get("instance_root") else None
            if is_member_root(iroot):
                continue  # a member's heartbeat — its hub is another record (or the default port)
            add(HubCandidate(_loopback(port), "heartbeat", instance_root=iroot, identity=hb["identity"], pid=hb["pid"]))
    add(HubCandidate(_loopback(DEFAULT_PORT), "default"))
    return out


# ── credentials ───────────────────────────────────────────────────────────────


def _read_token_file(path: Path) -> str | None:
    try:
        tok = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tok or None


def fleet_token_files(cand: HubCandidate) -> list[Path]:
    """Where the fleet service token for ``cand`` could live, most specific first: the
    candidate's own instance root, this shell's instance root, then every known box root
    (the desktop's instance root IS its box root)."""
    roots: list[Path] = []
    if cand.instance_root is not None:
        roots.append(cand.instance_root)
    roots.append(instance_paths().instance_root)
    roots.extend(known_box_roots())
    files: list[Path] = []
    for r in roots:
        p = r / "workspaces" / FLEET_TOKEN_FILE
        if p not in files:
            files.append(p)
    return files


def token_chain(cand: HubCandidate, *, explicit: str | None = None) -> Iterator[str | None]:
    """Credentials to try for ``cand``, in order, each yielded once; ends with ``None``
    (open mode — an unset-auth instance accepts no bearer at all). Never logs a value."""
    seen: set[str] = set()

    def once(tok: str | None) -> Iterator[str]:
        if tok and tok not in seen:
            seen.add(tok)
            yield tok

    yield from once((explicit or "").strip() or None)
    yield from once(os.environ.get(ENV_TOKEN, "").strip() or None)
    for f in fleet_token_files(cand):
        yield from once(_read_token_file(f))
    yield from once(os.environ.get(ENV_OPERATOR_BEARER, "").strip() or None)
    yield None


# ── the client ────────────────────────────────────────────────────────────────


def _slug_of(path: str) -> str | None:
    """``/agents/<slug>/…`` → ``<slug>``; anything else → None."""
    if not path.startswith("/agents/"):
        return None
    rest = path[len("/agents/") :]
    return rest.split("/", 1)[0] or None


class HubClient:
    """A thin, synchronous client over the hub routes the deck needs. Bounded timeouts;
    typed errors; the credential is sent as a Bearer and never surfaced."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        timeout: httpx.Timeout = _TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = normalize_url(url)
        self._token = token or None
        self._client = httpx.Client(base_url=self.url, timeout=timeout, transport=transport)

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _request(self, method: str, path: str, *, json_body: Any = None) -> Any:
        try:
            r = self._client.request(method, path, json=json_body, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HubUnreachable(self.url, f"{self.url} did not answer ({type(exc).__name__})") from exc
        if r.status_code == 401:
            slug = _slug_of(path)
            if slug:
                raise MemberUnauthorized(self.url, slug, f"member {slug!r} rejected the credential the hub attached")
            raise HubUnauthorized(self.url, f"{self.url} rejected the credential")
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = str(body.get("detail") if isinstance(body, dict) else body)
            except ValueError:
                detail = r.text[:300]
            raise HubRequestError(self.url, r.status_code, detail or r.reason_phrase)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # ── probes ──

    def agent_card(self) -> dict | None:
        """The hub's public agent card, or None when nothing protoAgent-shaped answers.
        Unauthenticated by design (``/.well-known/`` is a public prefix) — this is the
        "is there a hub here at all?" probe, the same one fleet discovery uses."""
        try:
            # No Authorization header on purpose: the card is public and the probe must
            # answer "is anything here?" independent of whether our credential is right.
            r = self._client.get("/.well-known/agent-card.json", timeout=_PROBE_TIMEOUT)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            card = r.json()
        except ValueError:
            return None
        return card if isinstance(card, dict) else None

    # ── fleet control plane (ADR 0042 §B) ──

    def fleet(self) -> list[dict]:
        data = self._request("GET", "/api/fleet")
        agents = (data or {}).get("agents") if isinstance(data, dict) else None
        return list(agents or [])

    def start(self, name: str) -> dict:
        return self._request("POST", f"/api/fleet/{quote(name, safe='')}/start") or {}

    def stop(self, name: str) -> dict:
        return self._request("POST", f"/api/fleet/{quote(name, safe='')}/stop") or {}

    def down(self) -> dict:
        return self._request("POST", "/api/fleet/down") or {}

    def runtime_status(self) -> dict:
        return self._request("GET", "/api/runtime/status") or {}


# ── connect: the first hub we can actually read ───────────────────────────────


def roster_is_a_member(roster: list[dict]) -> bool:
    """The authoritative tell: a member's own ``/api/fleet`` host row is stamped
    ``member: True`` by the server (``supervisor._host_entry`` — it has a ``workspace.yaml``
    at its instance root). A hub's host row never is."""
    host = next((a for a in roster if isinstance(a, dict) and a.get("host")), None)
    return bool(host and host.get("member"))


@dataclass
class Connection:
    client: HubClient
    candidate: HubCandidate
    card: dict
    # The roster read that proved the credential — callers that only need `ls` use it
    # instead of paying for a second GET.
    roster: list[dict]


def connect(
    *,
    url: str | None = None,
    token: str | None = None,
    candidates: list[HubCandidate] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Connection:
    """Open the first candidate hub whose roster we can read.

    For each candidate: probe the public agent card (no credential); on a hit, walk
    :func:`token_chain` until ``GET /api/fleet`` succeeds. A hub that answers but rejects
    every credential is remembered so :class:`NoHub` can say "unauthorized at …" rather
    than "no hub" — those are different operator actions.
    """
    tried: list[str] = []
    unauthorized: list[str] = []
    members: list[str] = []
    explicit = bool(url)
    for cand in candidates if candidates is not None else discover_hubs(explicit_url=url):
        tried.append(cand.url)
        probe = HubClient(cand.url, transport=transport)
        try:
            card = probe.agent_card()
        finally:
            probe.close()
        if card is None:
            continue
        rejected = False
        for tok in token_chain(cand, explicit=token):
            client = HubClient(cand.url, tok, transport=transport)
            try:
                roster = client.fleet()
            except HubUnauthorized:
                client.close()
                rejected = True
                continue
            except HubError:
                client.close()
                rejected = False
                break
            if roster_is_a_member(roster) and not explicit:
                # A member answers /api/fleet with a fleet-of-itself. Driving lifecycle
                # through it would "start"/"stop" members that do not exist there. Only
                # an operator who NAMED it (--hub) gets to talk to a member directly.
                client.close()
                members.append(cand.url)
                rejected = False
                break
            return Connection(client=client, candidate=cand, card=card, roster=roster)
        if rejected:
            unauthorized.append(cand.url)
    raise NoHub(tried, unauthorized, members)
