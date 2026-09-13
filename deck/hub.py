"""Hub client for the fleet deck (#3467, epic #3466) — how a terminal talks to a RUNNING hub.

Why a client and not the supervisor: ``protoagent fleet ls`` used to read ``fleet.json``
from whatever instance root the shell resolved and fabricate a "running" host row from its
own pid. Beside the desktop hub — which runs under ``PROTOAGENT_HOME=~/Library/Application
Support/studio.protolabs.protoagent`` — it reported a fleet of one while twelve workspaces
were registered and five were online. The hub's ``GET /api/fleet`` (ADR 0042) is the only
source of live truth, and in live mode every mutation must go through the hub so the hub
keeps owning its child processes: a ``supervisor.up`` next to a running hub spawns members
the hub cannot see (``_is_our_agent``). This resolves ADR 0075's open question 2 the lean
way: live hub first, badged disk fallback — and the fallback only when NOTHING answered.
A hub that answered but could not be opened (bad credential, timeout, 5xx) is an error the
operator has to see, never a reason to start driving processes from disk beside it.

Three jobs, in order:

1. **Find a hub.** ``discover_hubs()`` lists candidates by *evidence*, never by the
   environment the shell inherited: this instance's ``server.pid`` (``protoagent up``),
   the ``.instances/<pid>.json`` heartbeats every server writes under its box root
   (``infra.paths.register_instance``) — scanned across every box root this machine is
   known to use, including the desktop app's — and finally the default port. A MEMBER
   is a full server too (it writes a heartbeat and serves ``/api/fleet`` as a fleet of
   itself), so member instance roots are skipped by their ``workspace.yaml`` marker and a
   roster whose host row is ``member: True`` is refused unless the operator named it.
2. **Open it.** ``token_chain()`` yields credentials to try: ``--token`` / the env, then the
   hub's own fleet service token (``<instance root>/workspaces/.fleet-token``, ADR 0089 —
   operator tier on the hub and on every member through the proxy), then the operator
   bearer env, then no credential (open mode). ``connect()`` tries them until the roster
   reads. **Disk tokens and the operator bearer go to loopback only**: an explicit
   ``--hub`` on another host gets the explicit/env credential and nothing else, so a
   ``--hub evil:7870`` can never harvest this box's service token. Tokens are read from
   disk and never logged, printed, or echoed in errors; URL userinfo is stripped.
3. **Talk.** ``HubClient`` wraps the handful of routes the CLI needs with bounded timeouts
   and typed errors. A ``401``/``403`` on ``/agents/<slug>/…`` is that MEMBER's credential
   problem (``MemberUnauthorized``), never the hub's — the July 2026 remote audit
   (#1607/#1609) found the console conflating the two, and the deck must not repeat it.
   (Invariant that makes the split honest: ``connect()`` proves the credential on the hub
   itself first, so a later 401 on a member path cannot be the hub's own middleware.)
   Lifecycle calls get a long read budget: the hub boot-watches a start for up to 10 s,
   busy-waits a stop for up to 10 s, and ``/api/fleet/down`` does that for every running
   member in sequence.

Neutral by contract: httpx + ``infra`` only. Never imports ``server``, ``operator_api``,
or ``graph`` (the fleet CLI in ``graph/`` imports *this*). ``infra.paths`` is late-bound
through the thin delegates below so the test suite's instance-root isolation (which
patches ``infra.paths`` at runtime) reaches this module too.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from infra import paths as _paths

DEFAULT_PORT = 7870
ENV_TOKEN = "PROTOAGENT_HUB_TOKEN"
# The operator bearer env the auth middleware reads at boot (a2a_impl/auth.py).
ENV_OPERATOR_BEARER = "A2A_AUTH_TOKEN"
# Same file the hub mints (graph/fleet/service_token.py): <workspaces root>/.fleet-token.
FLEET_TOKEN_FILE = ".fleet-token"
# The spawn-time marker the supervisor writes into a MEMBER's instance root
# (graph/workspaces/manager.py::is_workspace_member).
WORKSPACE_MARKER = "workspace.yaml"
# The Tauri identifier — the desktop app sets PROTOAGENT_HOME = PROTOAGENT_BOX_ROOT = its
# per-user app-data dir for this id (apps/desktop/src-tauri/src/lib.rs), so a shell that
# wants the desktop hub's heartbeats and fleet token has to look there.
DESKTOP_APP_ID = "studio.protolabs.protoagent"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Read budgets. Reads are ordinary JSON; lifecycle waits on the hub's own blocking windows
# (graph/fleet/supervisor.py: _BOOT_WATCH_SECONDS=10 on start, stop timeout 8 s + 2 s
# kill grace; /api/fleet/down runs stop() sequentially over every running member).
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_PROBE_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
_LIFECYCLE_TIMEOUT = httpx.Timeout(60.0, connect=2.0)
_DOWN_PER_MEMBER_S = 15.0


# ── late-bound infra delegates (see module docstring) ─────────────────────────


def instance_paths():
    return _paths.instance_paths()


def box_root() -> Path:
    return _paths.box_root()


def data_home() -> Path:
    return _paths.data_home()


def pid_alive(pid: int) -> bool:
    return _paths.pid_alive(pid)


# ── errors ────────────────────────────────────────────────────────────────────


class HubError(RuntimeError):
    """Base for every hub-client failure; ``url`` names the hub involved."""

    def __init__(self, url: str, message: str):
        super().__init__(message)
        self.url = url


class HubUnreachable(HubError):
    """No usable answer from the hub within the bounded timeout (connect, read, or a
    torn connection mid-response)."""


class HubUnauthorized(HubError):
    """The HUB rejected our credential (a 401/403 on a hub path)."""


class MemberUnauthorized(HubError):
    """A MEMBER behind the proxy rejected the credential the hub attached (a 401/403 on
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
    """No candidate hub could be opened. The lists say what the search saw, and
    :attr:`answered` is the bit the CLI keys its fallback on: when ANY candidate answered
    its agent card, something is running, and driving processes from disk beside it is
    the wrong move — the operator needs the reason instead."""

    def __init__(
        self,
        tried: list[str],
        unauthorized: list[str],
        members: list[str] | None = None,
        failed: dict[str, str] | None = None,
    ):
        self.tried = list(tried)
        self.unauthorized = list(unauthorized)
        self.members = list(members or [])
        self.failed = dict(failed or {})
        if self.unauthorized:
            msg = (
                f"a hub answered at {', '.join(self.unauthorized)} but rejected every credential — "
                f"pass --token or set {ENV_TOKEN}"
            )
        elif self.failed:
            parts = "; ".join(f"{u}: {why}" for u, why in self.failed.items())
            msg = f"a hub answered but could not be read — {parts}"
        elif self.members:
            msg = (
                f"only fleet MEMBERS answered ({', '.join(self.members)}) — a member is a fleet of itself; "
                "point at its hub (or start one)"
            )
        elif self.tried:
            msg = f"no hub answered at {', '.join(self.tried)}"
        else:
            msg = "no hub candidates on this box"
        first = self.unauthorized or list(self.failed) or self.members or self.tried
        super().__init__(first[0] if first else "", msg)

    @property
    def answered(self) -> bool:
        """Something protoAgent-shaped answered somewhere (even if unusable)."""
        return bool(self.unauthorized or self.failed or self.members)


# ── candidates ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HubCandidate:
    """A place a hub might be, and the evidence for it."""

    url: str
    source: str  # "flag" | "pidfile" | "heartbeat" | "default"
    instance_root: Path | None = None
    identity: str = ""
    pid: int | None = None


_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]*@")


def redact_url(url: str | None) -> str:
    """``scheme://user:secret@host…`` → ``scheme://***@host…``. Applied to every operator-
    supplied URL before it can reach a message or a JSON field — including the REJECT
    path of :func:`normalize_url`, where the raw input is all we have."""
    u = (url or "").strip()
    if "://" not in u:
        u = f"http://{u}"
    return _USERINFO_RE.sub("***@", u)


def normalize_url(url: str) -> str:
    """``host:port`` → ``http://host:port``; trailing slash dropped; userinfo STRIPPED (a
    ``--hub http://user:secret@host`` must not echo the secret in any message). Raises
    ``ValueError`` on anything that is not a usable absolute URL — with the userinfo
    redacted in the message too, since the reject path is exactly where a mangled
    credential-bearing URL ends up."""
    u = (url or "").strip()
    if not u.strip("/"):
        raise ValueError("hub url is empty")
    if "://" not in u:
        u = f"http://{u}"
    try:
        parsed = httpx.URL(u)
    except httpx.InvalidURL as exc:
        # httpx's message can carry the offending text; keep only its class.
        raise ValueError(f"invalid hub url: {redact_url(url)!r} ({type(exc).__name__})") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"invalid hub url: {redact_url(url)!r}")
    clean = parsed.copy_with(username=None, password=None, path="", query=None, fragment=None)
    return str(clean).rstrip("/")


def is_loopback(url: str) -> bool:
    try:
        host = httpx.URL(url).host.strip("[]").lower()
    except httpx.InvalidURL:
        return False
    return host in _LOOPBACK_HOSTS


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


def is_member_root(instance_root: Path | None) -> bool:
    """True when ``instance_root`` is a fleet MEMBER's (spawned by some hub's supervisor)."""
    if instance_root is None:
        return False
    try:
        return (instance_root / WORKSPACE_MARKER).is_file()
    except OSError:
        return False


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
    (open mode — an unset-auth instance accepts no bearer at all). Never logs a value.

    This box's fleet service tokens and its operator bearer are LOOPBACK-ONLY: a hub on
    another host gets the explicit ``--token`` / ``PROTOAGENT_HUB_TOKEN`` and nothing else.
    Anything that returns a 200 agent card must not be able to harvest local credentials."""
    seen: set[str] = set()

    def once(tok: str | None) -> Iterator[str]:
        if tok and tok not in seen:
            seen.add(tok)
            yield tok

    yield from once((explicit or "").strip() or None)
    yield from once(os.environ.get(ENV_TOKEN, "").strip() or None)
    if is_loopback(cand.url):
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


class InsecureHub(HubError):
    """A credential would travel in cleartext: a non-loopback ``http://`` hub with a
    bearer set. Refused unless the operator opted in (``--insecure-http``, e.g. a tailnet
    URL that is encrypted underneath but cannot be told apart from a LAN one)."""


def credential_allowed(url: str, token: str | None, *, insecure_http: bool) -> None:
    """Raise :class:`InsecureHub` when sending ``token`` to ``url`` would be cleartext
    off-box (CWE-319). Loopback ``http://`` and any ``https://`` are always fine; a
    token-less client never carries anything worth protecting."""
    if not token or insecure_http:
        return
    if is_loopback(url):
        return
    if str(httpx.URL(url).scheme).lower() != "https":
        raise InsecureHub(
            url,
            f"refusing to send a credential over plain http to {httpx.URL(url).host} — "
            "use an https:// hub URL, or pass --insecure-http for a link you know is encrypted (a tailnet)",
        )


def _expect_dict(url: str, value: Any, what: str) -> dict:
    """Every route the deck calls answers a JSON object. A 2xx with anything else (a
    proxy's HTML, an empty body where one is required) is a hub error, not a success —
    ``start`` must never report a member started on missing data."""
    if isinstance(value, dict):
        return value
    raise HubError(url, f"malformed {what} response from {url} ({type(value).__name__})")


class HubClient:
    """A thin, synchronous client over the hub routes the deck needs. Bounded timeouts;
    typed errors; the credential is sent as a Bearer and never surfaced. Redirects are
    NOT followed — a 3xx is an error, so a bearer can never be replayed to a third host."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        timeout: httpx.Timeout = _TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        insecure_http: bool = False,
    ):
        self.url = normalize_url(url)
        self._token = token or None
        credential_allowed(self.url, self._token, insecure_http=insecure_http)
        self._client = httpx.Client(base_url=self.url, timeout=timeout, transport=transport, follow_redirects=False)

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _request(self, method: str, path: str, *, json_body: Any = None, timeout: httpx.Timeout | None = None) -> Any:
        try:
            r = self._client.request(
                method,
                path,
                json=json_body,
                headers=self._headers(),
                **({"timeout": timeout} if timeout is not None else {}),
            )
        except httpx.TransportError as exc:  # connect / read timeout, refused, torn mid-response
            raise HubUnreachable(self.url, f"{self.url} did not answer ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:  # anything else httpx can raise — never a raw traceback
            raise HubError(self.url, f"{self.url}: {type(exc).__name__}") from exc
        if 300 <= r.status_code < 400:
            raise HubRequestError(self.url, r.status_code, f"redirect to {r.headers.get('location', '?')!r} not followed")
        if r.status_code in (401, 403):
            slug = _slug_of(path)
            if slug:
                raise MemberUnauthorized(self.url, slug, f"member {slug!r} rejected the credential the hub attached")
            raise HubUnauthorized(self.url, f"{self.url} rejected the credential ({r.status_code})")
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("detail"), str) and body["detail"].strip():
                detail = body["detail"].strip()
            elif body is not None and not isinstance(body, dict):
                detail = str(body)[:300]
            if not detail:
                detail = (r.text or "").strip()[:300] or r.reason_phrase or "error"
            raise HubRequestError(self.url, r.status_code, detail)
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
        data = _expect_dict(self.url, self._request("GET", "/api/fleet"), "roster")
        agents = data.get("agents")
        if not isinstance(agents, list):
            raise HubError(self.url, f"malformed roster response from {self.url} (no agents list)")
        return [a for a in agents if isinstance(a, dict)]

    def start(self, name: str) -> dict:
        res = self._request("POST", f"/api/fleet/{quote(name, safe='')}/start", timeout=_LIFECYCLE_TIMEOUT)
        return _expect_dict(self.url, res, "start")

    def stop(self, name: str) -> dict:
        res = self._request("POST", f"/api/fleet/{quote(name, safe='')}/stop", timeout=_LIFECYCLE_TIMEOUT)
        return _expect_dict(self.url, res, "stop")

    def down(self, running: int = 0) -> dict:
        """Stop every running member. The hub stops them one after another, each with
        its own grace window, so the read budget scales with how many are up."""
        budget = max(_LIFECYCLE_TIMEOUT.read or 0.0, _DOWN_PER_MEMBER_S * max(int(running), 0))
        res = self._request("POST", "/api/fleet/down", timeout=httpx.Timeout(budget, connect=2.0))
        return _expect_dict(self.url, res, "down")

    def runtime_status(self) -> dict:
        return _expect_dict(self.url, self._request("GET", "/api/runtime/status"), "runtime status")

    def telemetry_fleet(self) -> dict:
        """The hub-side rollup (spend / turns / flags per member, ADR 0006 fleet extension)."""
        return _expect_dict(self.url, self._request("GET", "/api/telemetry/fleet"), "telemetry")

    # ── per-member reads through the slug proxy (ADR 0042) ──

    @staticmethod
    def member_path(slug: str, rel: str) -> str:
        """The console's rule exactly (apps/web/src/lib/api.ts::memberPath): the reserved
        ``host`` slug is the hub itself (``/api/…``); any other member rides the proxy at
        ``/agents/<slug>/…``."""
        rel = rel if rel.startswith("/") else f"/{rel}"
        return rel if slug == "host" else f"/agents/{quote(slug, safe='')}{rel}"

    def member_get(self, slug: str, rel: str, **params: Any) -> Any:
        path = self.member_path(slug, rel)
        if params:
            path = f"{path}?{'&'.join(f'{quote(str(k), safe=chr(0))}={quote(str(v), safe=chr(0))}' for k, v in params.items())}"
        return self._request("GET", path)

    def member_runtime_status(self, slug: str) -> dict:
        return _expect_dict(self.url, self.member_get(slug, "/api/runtime/status"), f"{slug} runtime status")

    def diagnostics_logs(self, slug: str, lines: int = 200) -> dict:
        """A bounded, redacted tail of the member's in-process log ring (#3168)."""
        return _expect_dict(self.url, self.member_get(slug, "/api/diagnostics/logs", lines=lines), f"{slug} logs")

    def diagnostics_sessions(self, slug: str, limit: int = 50) -> dict:
        """Newest-first session inventory from the member's task store (#3171 slice 1)."""
        return _expect_dict(self.url, self.member_get(slug, "/api/diagnostics/sessions", limit=limit), f"{slug} sessions")

    def console_href(self, slug: str) -> str:
        """Where the browser console shows this member (slug routing, ADR 0042): the host
        at ``/app/``, a member at ``/app/agent/<slug>/``."""
        return f"{self.url}/app/" if slug == "host" else f"{self.url}/app/agent/{quote(slug, safe='')}/"


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
    roster: list[dict] = field(default_factory=list)


def connect(
    *,
    url: str | None = None,
    token: str | None = None,
    candidates: list[HubCandidate] | None = None,
    transport: httpx.BaseTransport | None = None,
    insecure_http: bool = False,
) -> Connection:
    """Open the first candidate hub whose roster we can read.

    For each candidate: probe the public agent card (no credential); on a hit, walk
    :func:`token_chain` until ``GET /api/fleet`` succeeds. Every outcome short of success
    is recorded on the :class:`NoHub` — rejected credentials, a hub that answered but whose
    roster read failed (timeout, 5xx, torn connection), a member answering as a fleet of
    itself — so the CLI can tell "nothing is running" from "something is, and here is why
    I could not use it". Only the former may fall back to disk.
    """
    tried: list[str] = []
    unauthorized: list[str] = []
    members: list[str] = []
    failed: dict[str, str] = {}
    for cand in candidates if candidates is not None else discover_hubs(explicit_url=url):
        tried.append(cand.url)
        with HubClient(cand.url, transport=transport) as probe:
            card = probe.agent_card()
        if card is None:
            # No card — but if the evidence for this candidate is a LIVE server process
            # (pidfile / heartbeat), something IS running there and is merely stalled or
            # still booting. That must not read as "nothing answered" or the CLI would
            # fall back to driving processes from disk beside it.
            if cand.pid and pid_alive(cand.pid):
                failed[cand.url] = f"a live server process (pid {cand.pid}) did not answer its agent card"
            continue
        rejected = False
        for tok in token_chain(cand, explicit=token):
            try:
                client = HubClient(cand.url, tok, transport=transport, insecure_http=insecure_http)
            except InsecureHub as exc:
                failed[cand.url] = str(exc)
                break
            try:
                roster = client.fleet()
            except HubUnauthorized:
                client.close()
                rejected = True
                continue
            except HubError as exc:
                client.close()
                rejected = False
                failed[cand.url] = str(exc)
                break
            except BaseException:
                client.close()
                raise
            if roster_is_a_member(roster):
                # A member answers /api/fleet with a fleet-of-itself. Driving lifecycle
                # through it would "start"/"stop" members that do not exist there — so a
                # member is refused for EVERY candidate, an explicit --hub included: the
                # fleet a member belongs to lives on its hub, and that is what to name.
                client.close()
                members.append(cand.url)
                rejected = False
                break
            return Connection(client=client, candidate=cand, card=card, roster=roster)
        if rejected:
            unauthorized.append(cand.url)
    raise NoHub(tried, unauthorized, members, failed)
