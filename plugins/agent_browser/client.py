"""A browser client for OTHER plugins — core's hardened agent-browser path, behind a service.

A plugin that drives a browser (ebay-plugin's price checks, a scraper, a form bot) used to shell
out to the `agent-browser` CLI itself with ``subprocess.run``: no output cap, no process-group
kill, no per-session lock, no CLI resolution or first-use download, no option guard — and every
failure came back as a string, so a stuck browser and a bot wall looked alike. This module hands
such a plugin the same machinery the browser tools use, as a small typed API. It reaches it
through the service seam (ADR 0116), never an import::

    from graph import sdk
    make = getattr(sdk, "service", lambda _n: None)("agent_browser.client")
    if make is None:
        ...  # agent_browser is off, or this core predates the client — use your own fallback
    browser = make(session="ebay", profile="~/.protoagent/ebay-profile", headed=True,
                   own_tab="ebaytab", min_interval_s=1.5)
    r = browser.open("https://www.ebay.com/sch/i.html?_nkw=switch")
    if r.kind == "wall":      ...  # r.wall.kind / r.wall.label / r.error — hand it to the operator
    elif r.kind == "timeout": ...  # the browser didn't answer: stuck tab / slow page, NOT a wall
    elif not r.ok:            ...  # r.error says what failed and what to do
    browser.wait(selector="li.s-card", timeout_s=10)
    rows = browser.eval_json(SCRIPT).value

**Every method is synchronous and thread-safe** — call it from a sync tool body (LangChain runs
those in a worker thread) or wrap it in ``asyncio.to_thread``; never call it on the event loop.
Every method returns a :class:`Result` (``kind`` is a plain string, so a consumer needs no import
to branch on it) and never raises for an expected failure; ``Result.raise_for_error()`` turns a
failure into ``client.Error`` (``BrowserClientError``, with the same ``kind``) for code that
prefers exceptions.

What the client guarantees, each learned the hard way:

* **Runs through ``runner.run``** — core's byte cap, process-group kill and bounded joins; the
  CLI resolved like the tools resolve it (PATH > ``binary`` > the pinned download, fetched on
  first use).
* **Its own named session.** ``session`` is the plugin's choice; by default it is prefixed with
  this instance's name (``pa-<instance>-<h6>-<session>``) so two protoAgent instances never share
  one Chrome (#4921). ``instance_scoped=False`` keeps the name verbatim — for a plugin whose
  operator already signed a fixed-name session in. Pinned through ``runtime.cli_env``, with an
  optional idle reaper.
* **The same per-session guard as the tools.** A client on the tools' own session can't
  interleave with a ``browser_fill``; on its own session it never waits for them.
* **``open(url)`` carries the launch flags every time, and never runs a URL-less ``open``.**
  Launch options are daemon-level and reconciled per launch request: on ``open <url>`` the CLI
  sends ONE full-options launch (same options → reused; different → relaunched on the same
  profile; no daemon → started with them). A URL-less ``open`` with flags sends that launch AND
  a second, flagless one, and the daemon relaunches Chrome onto a throwaway temp profile — the
  bug that kept ebay-plugin's profile empty for five weeks. ``open("")`` is refused.
* **Explicit wait timeouts below the CLI's 30 s IPC read timeout.** The daemon's default wait is
  30 s, equal to the CLI's socket read timeout; a wait that never succeeds then reads as a
  transient error and the CLI RE-SENDS it, five times (``connection.rs`` ``send_command``) —
  ~150 s for one failed wait. ``wait`` always sends a timeout (default 10 s, max 25 s).
* **A navigation timeout short of the CLI's retry.** A navigation the daemon never answers (a
  wedged tab) is re-sent the same way; ``nav_timeout_s`` (default 45 s) kills it after the first
  attempt, and ``open`` recovers once — replacing its own tab when it has one — before reporting
  a typed ``timeout``.
* **Optional tab ownership** (``own_tab="<label>"``): the client navigates only a labelled tab of
  its own. agent-browser 0.27.1 runs every command on the ACTIVE tab and makes any newly
  discovered target active, so in a headed window shared with the operator a "current tab"
  could be Chrome's Gemini side panel (a ``webview``; navigating it fails
  ``net::ERR_BLOCKED_BY_CLIENT``) or one of the operator's pages. Only Chrome's own panels are
  ever closed, and the client's own tab when it is wedged — never an operator page. Proven in
  ebay-plugin 0.3.2-0.3.3 and generalised here.
* **Wall detection** (``walls.py``): ``open`` probes the landed page and returns ``kind="wall"``
  for a bot check / block page / sign-in wall, re-probing a transient JS challenge first.
  Nothing tries to get past one.
* **The argv option guard** (``runtime.bad_operand``) on every caller-supplied operand.
* **``form_read(scope)``** runs the same in-page form reader as ``browser_form_read`` and hands
  back the field list as DATA (``Result.value``), so a plugin can verify a form against the real
  page instead of trusting a read-back the model passed in (it can trim one).

**The agent's own browser.** ``make(session=...)`` is always a session of the plugin's own. To
act on the page the agent's ``browser_*`` tools are driving in THIS chat (verify what it
filled), use ``make.for_agent_session(state)`` from inside a tool call, passing the tool's
injected graph state (``state: Annotated[Any, InjectedState]``): it resolves the session name
exactly as the tools do (``session_scope: shared`` → the instance's main session;
``per_session`` → the name derived from ``state["session_id"]``), takes the same per-session
guard, and sends the agent_browser plugin's own launch flags, so it can't relaunch the agent's
Chrome with different options. ``close()`` is refused on it — that browser isn't the plugin's.

No stealth or anti-detection: the client sends exactly the launch options the plugin asks for
(``headed``, ``profile``, ``user_agent``, Chrome ``browser_args``) and adds none of its own.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import boundaries, forms, preflight, runner, runtime, walls

log = logging.getLogger("protoagent.plugins.agent_browser")

# Result kinds — plain strings so a consumer branches without importing anything.
OK = "ok"
TIMEOUT = "timeout"            # the browser/CLI didn't answer in time (stuck tab, very slow page)
WALL = "wall"                  # a bot check / block page / sign-in wall answered (Result.wall)
CLI_ERROR = "cli_error"        # the CLI ran and failed (bad selector, no page, Chrome refused …)
SCRIPT_ERROR = "script_error"  # eval_json: the page script threw, or its output isn't JSON
UNAVAILABLE = "unavailable"    # no CLI to run (not installed / still downloading / can't start)
BAD_ARGUMENT = "bad_argument"  # refused before running (an option-shaped operand, a bad call)
TOO_LARGE = "too_large"        # the output passed the byte cap (non-truncatable command)
KINDS = (OK, TIMEOUT, WALL, CLI_ERROR, SCRIPT_ERROR, UNAVAILABLE, BAD_ARGUMENT, TOO_LARGE)

# The CLI re-sends a command whose reply hasn't come in 30 s (its socket read timeout) up to 5x,
# so every wait below carries its own timeout, kept under that 30 s.
_WAIT_MAX_S = 25.0
_LOAD_STATES = ("load", "domcontentloaded", "networkidle")
_WALL_RECHECKS = 2
_WALL_RECHECK_S = 2.5
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SESSION_MAX = 24        # the CLI's socket is <dir>/<name>.sock; a unix socket path caps near 104 bytes
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
# `tab <label>` shares its slot with the CLI's own tab subcommands and `t<N>` tab ids, so a label
# spelled like one would run a different command (QA, #4941).
_TAB_WORDS = frozenset({"list", "new", "close"})
_TAB_ID_RE = re.compile(r"^t\d+$", re.I)


def _valid_label(label: str) -> bool:
    return bool(_LABEL_RE.match(label)) and label.lower() not in _TAB_WORDS and not _TAB_ID_RE.match(label)
# Chrome's own targets that take the active slot: the Gemini side panel, as a `webview`.
_GEMINI_PANEL = "gemini.google.com/glic"
_BLOCKED = "ERR_BLOCKED_BY_CLIENT"
# Daemon-side timeouts and the CLI's give-up after its IPC retries.
_TIMEOUT_TEXT = re.compile(r"timed out|timeout|daemon may be busy or unresponsive", re.I)


class BrowserClientError(RuntimeError):
    """A failed :class:`Result`, raised by :meth:`Result.raise_for_error`. ``kind`` is the
    result's kind; ``result`` the Result itself. Reachable as ``client.Error``."""

    def __init__(self, result: Result):
        super().__init__(result.error or result.kind)
        self.kind = result.kind
        self.result = result


@dataclass(frozen=True)
class Result:
    """The outcome of one client call.

    ``kind`` is one of :data:`KINDS`. On ``ok``: ``text`` is the CLI's text output and ``value``
    the parsed payload where the method has one (``eval_json``'s JSON, ``open``'s
    ``{"url","title"}``, ``tabs``' list). On failure: ``error`` is a readable sentence that says
    what to do next, safe to show an operator or a model verbatim; ``wall`` is set for
    ``kind == "wall"`` (``.kind`` / ``.label`` / ``.url`` / ``.transient``). ``truncated`` marks a
    ``get_text`` / ``snapshot`` cut at the byte cap. ``seconds`` is the wall-clock time spent."""

    kind: str
    text: str = ""
    value: Any = None
    error: str = ""
    wall: walls.Wall | None = None
    truncated: bool = False
    seconds: float = 0.0
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == OK

    def raise_for_error(self) -> Result:
        """Return ``self`` when ok, else raise :class:`BrowserClientError`."""
        if not self.ok:
            raise BrowserClientError(self)
        return self

    def to_dict(self) -> dict:
        """JSON-friendly form (``wall`` flattened) — for a tool that returns JSON."""
        d = {"kind": self.kind, "ok": self.ok, "seconds": round(self.seconds, 2)}
        if self.error:
            d["error"] = self.error
        if self.wall is not None:
            d["wall"] = {"kind": self.wall.kind, "label": self.wall.label, "url": self.wall.url,
                         "transient": self.wall.transient}
        if self.truncated:
            d["truncated"] = True
        return d


class _Host:
    """What the agent_browser plugin's own config decides for every client: which CLI runs, the
    first-use download, the byte cap, and the setup banner's refresh. ``cfg`` is a dict or a
    callable returning the CURRENT config, read on every command, so a Settings change applies
    to clients already built."""

    def __init__(self, cfg=None, refresh_gaps=None):
        self._cfg = cfg if callable(cfg) else (lambda: cfg or {})
        self.refresh_gaps = refresh_gaps

    def _live(self) -> dict:
        try:
            return self._cfg() or {}
        except Exception:  # noqa: BLE001 — a config read must never fail a browser call
            return {}

    @property
    def binary(self) -> str:
        return str(self._live().get("binary") or "agent-browser")

    @property
    def autofetch(self) -> bool:
        return runtime.flag(self._live(), "cli_autofetch", True)

    @property
    def max_bytes(self) -> int:
        return runtime.number(self._live(), "max_response_bytes", 200_000, cast=int, positive=True)


def _envelope(text: str) -> dict | None:
    """The CLI's ``--json`` envelope ``{"success", "data", "error"}`` — the LAST JSON line of
    stdout (a warning or progress line may precede it)."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "success" in obj:
                return obj
    return None


def _decode_value(value: Any) -> Any:
    """A page script that returned ``JSON.stringify(x)`` gives a string; decode it once more.
    A plain non-JSON string is a legitimate result and is handed back as-is."""
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return json.loads(value)
    return value


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1].lstrip("✗ ").strip() if lines else ""


class BrowserClient:
    """One plugin's handle on one agent-browser session. Build it through the
    ``agent_browser.client`` service (``make(**options)``), keep it for the plugin's lifetime
    (the pacing clock and tab bookkeeping live on it), and share it across that plugin's tools.

    Options (all keyword):

    * ``session`` — required; letters, digits, ``-``, ``_``, max 24 chars.
    * ``instance_scoped`` (True) — prefix the instance's name so instances never share a Chrome.
    * ``profile`` — a Chrome profile dir (persistent sign-in) or profile name; ``headed`` (False);
      ``user_agent``; ``browser_args`` — extra Chrome flags (each starting ``--``, no commas).
    * ``own_tab`` — a tab label to own (see the module doc); blank = use the active tab.
    * ``timeout_s`` (30) — per command; ``nav_timeout_s`` (45) — per ``open``.
    * ``min_interval_s`` (0) — minimum gap between navigations (politeness pacing).
    * ``idle_timeout_min`` (0 = never) — close this session's browser after that long idle; it
      applies when the client STARTS the daemon (the CLI reads it once, at spawn).
    """

    Error = BrowserClientError
    KINDS = KINDS

    def __init__(self, *, session: str, instance_scoped: bool = True, profile: str = "",
                 headed: bool = False, user_agent: str = "", browser_args=(), own_tab: str = "",
                 timeout_s: float = 30.0, nav_timeout_s: float = 45.0, min_interval_s: float = 0.0,
                 idle_timeout_min: float = 0.0, host: _Host | None = None):
        session = str(session or "").strip()
        if not _SESSION_RE.match(session) or len(session) > _SESSION_MAX:
            raise ValueError(f"session must be 1-{_SESSION_MAX} letters, digits, '-' or '_' "
                             f"(starting with a letter or digit); got {session!r}")
        self.name = session
        self.session = f"{runtime.instance_session_name()}-{session}" if instance_scoped else session
        self.profile = str(profile or "").strip()
        if self.profile and runtime.OPTION_LIKE.match(self.profile):
            raise ValueError(f"profile {self.profile!r} looks like a command-line option")
        self.headed = bool(headed)
        self.user_agent = str(user_agent or "").strip()
        if self.user_agent and runtime.OPTION_LIKE.match(self.user_agent):
            raise ValueError("user_agent looks like a command-line option")
        args = [str(a).strip() for a in (browser_args or ()) if str(a).strip()]
        for a in args:
            if not a.startswith("--") or "," in a:
                raise ValueError(f"browser_args entries are Chrome flags ('--name[=value]', no "
                                 f"commas); got {a!r}")
        self.browser_args = tuple(args)
        own_tab = str(own_tab or "").strip()
        if own_tab and not _valid_label(own_tab):
            raise ValueError(f"own_tab must be a short label (a letter, then letters, digits, "
                             f"'-' or '_'; not list/new/close or a t<N> tab id); got {own_tab!r}")
        self.own_tab = own_tab
        self.timeout_s = max(1.0, float(timeout_s))
        self.nav_timeout_s = max(5.0, float(nav_timeout_s))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.idle_ms = int(float(idle_timeout_min) * 60_000) if float(idle_timeout_min) > 0 else 0
        self._host = host or _Host()
        self._last_nav = 0.0
        self._pace_lock = threading.Lock()
        # Set by for_agent_session(): this client drives the agent's tools' session, not one of
        # the plugin's own — launch flags and the idle reaper then come from agent_browser's
        # config (what the tools send), and close() is refused. ``agent_chat`` marks a per-chat
        # (per_session) browser rather than the instance's main one.
        self.agent_session = False
        self.agent_chat = False

    @classmethod
    def for_agent_session(cls, state: Any = None, *, session_id: str | None = None,
                          timeout_s: float = 30.0, nav_timeout_s: float = 45.0,
                          host: _Host | None = None) -> BrowserClient:
        """A client on the SAME agent-browser session the ``browser_*`` tools use for the
        calling chat — see the module doc. ``state``: the calling tool's injected graph state
        (authoritative, like the tools: its ``session_id``, even an empty one). Without it,
        ``session_id`` or the tracing context is used — but note the tracing id is EMPTY inside
        a tool body, so under ``session_scope: per_session`` pass ``state`` (or ``session_id``);
        with neither this raises ``ValueError`` rather than silently reading another page."""
        host = host or _Host()
        cfg = host._live()
        scope = str(cfg.get("session_scope") or "shared").strip().lower()
        name, chat = _agent_session_name(scope, state, session_id)
        # Built with a placeholder name, then pointed at the tools' name verbatim: that name is
        # the tools' (or the operator's AGENT_BROWSER_SESSION), not bound by the 24-char rule
        # for a plugin's own session.
        c = cls(session="agent", instance_scoped=False, timeout_s=timeout_s,
                nav_timeout_s=nav_timeout_s, host=host)
        c.session, c.agent_session, c.agent_chat = name, True, chat
        return c

    def __repr__(self) -> str:
        tag = ", agent_session=True" if self.agent_session else ""
        return f"BrowserClient(session={self.session!r}, own_tab={self.own_tab!r}{tag})"

    # ── plumbing ─────────────────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def hold(self):
        """Hold this session for a multi-step sequence (open → wait → read) so no other client
        thread — or a browser tool on the same session — runs in between. Re-entrant per thread;
        every method takes it on its own."""
        g, owner = runner.guard(self.session), ("client", threading.get_ident())
        g.acquire(owner)
        try:
            yield self
        finally:
            g.release(owner)

    def launch_flags(self) -> list[str]:
        """The daemon-level options sent with every ``open <url>`` and every tab command. On the
        agent's session they are the agent_browser plugin's own (``runtime.launch_flags``), the
        same the tools send, so this client never relaunches the agent's Chrome differently."""
        if self.agent_session:
            return runtime.launch_flags(self._host._live())
        f: list[str] = []
        if self.headed:
            f.append("--headed")
        if self.profile:
            f += ["--profile", self.profile]
        if self.user_agent:
            f += ["--user-agent", self.user_agent]
        if self.browser_args:
            f += ["--args", ",".join(self.browser_args)]
        return f

    def _exec(self, *args: str, stdin: str | None = None, timeout: float | None = None,
              env_extra: dict | None = None) -> tuple[runner.RunResult | None, Result | None]:
        """Run one CLI command. Returns ``(run, None)`` when it ran (any exit code), or
        ``(None, failure)`` when it couldn't (no CLI)."""
        host = self._host
        cmd = " ".join(a for a in args[:2] if not a.startswith("--")) or (args[0] if args else "")
        exe = preflight.cli_for_run(host.binary, autofetch=host.autofetch,
                                    on_done=host.refresh_gaps if callable(host.refresh_gaps) else None)
        idle = (runtime.idle_timeout_ms(host._live(), chat=self.agent_chat) if self.agent_session
                else self.idle_ms)
        env = runtime.cli_env(self.session, idle)
        if env_extra:
            env = {**env, **env_extra}
        res = runner.run([exe, *args], env=env, timeout=timeout or self.timeout_s,
                         max_bytes=host.max_bytes, stdin=stdin)
        if res.launch_error is not None:
            if callable(host.refresh_gaps):
                with contextlib.suppress(Exception):
                    host.refresh_gaps()
            e = res.launch_error
            found = preflight.resolve_binary(host.binary)
            why = (f"the {host.binary!r} CLI isn't installed (or is still downloading) — install it "
                   f"with `{preflight.INSTALL_HINT}`; the console's setup banner says so too"
                   if isinstance(e, FileNotFoundError) and not found else
                   f"the {host.binary!r} CLI was found but could not be started "
                   f"({e.strerror or e.__class__.__name__})")
            return None, Result(UNAVAILABLE, error=f"Browser unavailable: {why}.", command=cmd,
                                seconds=res.seconds)
        return res, None

    def _command(self, *args: str, stdin: str | None = None, timeout: float | None = None,
                 env_extra: dict | None = None, json_out: bool = False, truncatable: bool = False) -> Result:
        """Run a command and map it to a :class:`Result`. ``json_out`` adds ``--json`` and parses
        the envelope into ``value``; ``truncatable`` returns the first part of an over-cap output
        (``truncated=True``) instead of a ``too_large`` failure."""
        argv = ("--json", *args) if json_out else args
        res, failed = self._exec(*argv, stdin=stdin, timeout=timeout, env_extra=env_extra)
        if failed is not None:
            return failed
        cmd = " ".join(a for a in args[:2] if not a.startswith("--"))
        limit = timeout or self.timeout_s
        if res.timed_out:
            return Result(TIMEOUT, command=cmd, seconds=res.seconds, error=(
                f"The browser did not answer `{cmd}` within {limit:g}s (session {self.session!r}). "
                "The browser or its tab is stuck, or the page is extremely slow — this is NOT a bot "
                "wall or a sign-in problem. Retry once; if it repeats, the operator should check the "
                "browser window."))
        if res.overflow:
            if truncatable and res.out.strip():
                text = res.out.decode("utf-8", "ignore")
                cut = text.rfind("\n")
                text = boundaries.close_cut((text[:cut] if cut >= len(text) // 2 else text).rstrip())
                return Result(OK, text=text, truncated=True, command=cmd, seconds=res.seconds)
            return Result(TOO_LARGE, command=cmd, seconds=res.seconds, error=(
                f"`{cmd}` produced more than {self._host.max_bytes} bytes; narrow it (a smaller "
                "selector, or return less from the script)."))
        out, err = res.stdout(), res.stderr()
        env = _envelope(out) if json_out else None
        if res.returncode == 0 and (not json_out or (env is not None and env.get("success"))):
            data = env.get("data") if env else None
            return Result(OK, text=out, value=data, command=cmd, seconds=res.seconds)
        reason = (str(env.get("error") or "") if env else "") or _last_line(err) or _last_line(out) \
            or f"exit code {res.returncode}"
        # On the agent's own session the launch flags (so the profile and session_scope) are the
        # agent_browser plugin's config, not this client's (QA, #4957): diagnose with those.
        lock_cfg = self._host._live() if self.agent_session else {"profile": self.profile}
        if (locked := runtime.profile_in_use_error(err + "\n" + reason, lock_cfg, self.session)):
            return Result(CLI_ERROR, error=locked.removeprefix("Error: "), command=cmd, seconds=res.seconds)
        # A page script's own error text can say "timeout" — that is the script's, not the browser's.
        kind = TIMEOUT if _TIMEOUT_TEXT.search(reason) and "Evaluation error" not in reason else CLI_ERROR
        return Result(kind, text=out, error=f"`{cmd}` failed: {reason[:500]}", command=cmd,
                      seconds=res.seconds)

    def _refuse(self, **values) -> Result | None:
        bad = runtime.bad_operand(**values)
        return Result(BAD_ARGUMENT, error=bad.removeprefix("Error: ")) if bad else None

    def _pace(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._pace_lock:
            gap = time.monotonic() - self._last_nav
            if gap < self.min_interval_s:
                time.sleep(self.min_interval_s - gap)
            self._last_nav = time.monotonic()

    # ── tabs ─────────────────────────────────────────────────────────────────────────────
    def tabs(self) -> Result:
        """``value`` = the session's tabs: ``[{"tabId", "label", "type", "url", "title",
        "active"}]``. Starts the browser (with the launch flags) when none is running."""
        with self.hold():
            r = self._command("tab", "list", *self.launch_flags(), json_out=True)
            if not r.ok:
                return r
            tabs = (r.value or {}).get("tabs") if isinstance(r.value, dict) else None
            return Result(OK, text=r.text, value=tabs if isinstance(tabs, list) else [],
                          command=r.command, seconds=r.seconds)

    @staticmethod
    def is_panel(tab: dict) -> bool:
        """Chrome's own target that takes the active slot — the Gemini side panel (a ``webview``,
        or its URL). An ordinary ``page`` is never a panel, whatever its URL."""
        return str(tab.get("type") or "page") == "webview" or _GEMINI_PANEL in str(tab.get("url") or "")

    def close_panels(self) -> Result:
        """Close Chrome's panels (:meth:`is_panel`) in this session; ``value`` = the tab ids closed.
        Never closes a page."""
        with self.hold():
            listed = self.tabs()
            if not listed.ok:
                return listed
            closed = []
            for t in listed.value:
                if self.is_panel(t) and t.get("tabId") and \
                        self._command("tab", "close", str(t["tabId"]), *self.launch_flags()).ok:
                    closed.append(str(t["tabId"]))
            if closed:
                log.warning("[agent_browser] client %s closed Chrome panel target(s) %s that had taken "
                            "the active tab", self.session, closed)
            return Result(OK, value=closed)

    def focus_tab(self, label: str = "") -> Result:
        """Make the labelled tab (default ``own_tab``) active: ``value`` is ``"switched"`` (it
        existed), ``"created"`` (it had to be opened) or ``"recreated"`` (it existed but would not
        take focus — a hung tab — so it was closed and opened afresh). Launch flags ride along, so
        a dead daemon comes back with this client's profile."""
        label = str(label or self.own_tab).strip()
        if not _valid_label(label):
            return Result(BAD_ARGUMENT, error=f"tab label {label!r} is not a short label")
        flags = self.launch_flags()
        with self.hold():
            if (r := self._command("tab", label, *flags)).ok:
                return Result(OK, value="switched", seconds=r.seconds)
            if r.kind in (UNAVAILABLE, TIMEOUT):
                return r
            r = self._command("tab", "new", "--label", label, *flags)
            if r.ok:
                return Result(OK, value="created", seconds=r.seconds)
            if "already used" in r.error:
                # Our tab exists but would not take focus. Left alone, every navigation after this
                # fails until someone closes it by hand — so close OUR label and start a fresh one.
                self._command("tab", "close", label, *flags)
                r = self._command("tab", "new", "--label", label, *flags)
                if r.ok:
                    log.warning("[agent_browser] client %s: tab %r would not take focus — replaced it",
                                self.session, label)
                    return Result(OK, value="recreated", seconds=r.seconds)
            return r

    def _reset_own_tab(self) -> None:
        """After a timed-out navigation: close Chrome's panels and replace our own tab."""
        if not self.own_tab:
            return
        self.close_panels()
        flags = self.launch_flags()
        self._command("tab", "close", self.own_tab, *flags)
        self._command("tab", "new", "--label", self.own_tab, *flags)

    # ── navigation ───────────────────────────────────────────────────────────────────────
    def open(self, url: str, *, check_walls: bool = True, recover: bool = True) -> Result:
        """Navigate to ``url`` (launch flags attached; the browser starts, is reused, or relaunches
        to match). ``value`` = ``{"url", "title"}`` of the landed page.

        * ``kind="wall"`` — a bot check / block page / sign-in wall answered (``check_walls``);
          a transient JS challenge is re-probed for ~5 s first.
        * ``kind="timeout"`` — no answer within ``nav_timeout_s``. With ``recover`` (default) the
          client first retries ONCE, replacing its own tab when it has one.
        * With ``own_tab``, navigation goes to that tab; a Chrome panel that grabs the active slot
          mid-navigation (``ERR_BLOCKED_BY_CLIENT``) is closed and the navigation retried once.

        A blank ``url`` is refused (``bad_argument``): a URL-less ``open`` relaunches Chrome onto
        a temp profile. Use ``open("about:blank")`` for a blank page."""
        url = str(url or "").strip()
        if not url:
            return Result(BAD_ARGUMENT, error=(
                "open() needs a URL. A URL-less `open` makes agent-browser relaunch Chrome onto a "
                "throwaway temp profile; use open('about:blank') for an empty page."))
        if (bad := self._refuse(url=url)):
            return bad
        started = time.monotonic()
        with self.hold():
            self._pace()
            r = self._navigate(url)
            if r.kind == TIMEOUT and recover:
                log.warning("[agent_browser] client %s: open timed out after %.0fs — recovering once",
                            self.session, r.seconds)
                self._reset_own_tab()
                r = self._navigate(url)
                if r.kind == TIMEOUT:
                    r = Result(TIMEOUT, command="open", error=(
                        f"{url[:200]} did not load within {self.nav_timeout_s:g}s, twice (the second "
                        f"time {'in a fresh tab' if self.own_tab else 'after a retry'}). The browser is "
                        "stuck or the site is not answering — this is NOT a bot wall or a sign-in "
                        "problem. Don't loop on it: try again later, or have the operator check the "
                        "browser window."))
            if r.ok and check_walls:
                r = self._after_open(r, url)
            elif not r.ok and (wall := walls.classify_failure(r.error, url)) and not self.own_tab:
                r = Result(WALL, wall=wall, error=walls.message(wall).removeprefix("Error: "),
                           command="open")
            return Result(r.kind, text=r.text, value=r.value, error=r.error, wall=r.wall,
                          command="open", seconds=time.monotonic() - started)

    def _navigate(self, url: str) -> Result:
        if self.own_tab:
            focused = self.focus_tab()
            if not focused.ok:
                return Result(focused.kind, error=f"could not switch to the plugin's own browser tab "
                                                  f"{self.own_tab!r}: {focused.error}")
        r = self._command("open", url, *self.launch_flags(), json_out=True, timeout=self.nav_timeout_s)
        if not r.ok and self.own_tab and _BLOCKED in r.error:
            # A target took the active slot between the switch and the navigation (the Gemini
            # panel reopening). Close the panels, take the tab back, try exactly once more.
            self.close_panels()
            self.focus_tab()
            r = self._command("open", url, *self.launch_flags(), json_out=True, timeout=self.nav_timeout_s)
        return r

    def _after_open(self, opened: Result, url: str) -> Result:
        for attempt in range(_WALL_RECHECKS + 1):
            probe = self._command("eval", "--stdin", stdin=walls.PROBE_JS, json_out=True)
            parsed = _decode_value((probe.value or {}).get("result")) if probe.ok and isinstance(probe.value, dict) else None
            if not isinstance(parsed, dict):
                return opened  # an unreadable probe leaves the navigation's own result
            wall = walls.classify(parsed, url)
            if wall is None:
                if attempt == 0:
                    return opened
                return Result(OK, value={"url": parsed.get("url") or url, "title": parsed.get("title") or ""})
            if not wall.transient or attempt == _WALL_RECHECKS:
                return Result(WALL, wall=wall, value={"url": parsed.get("url") or url,
                                                      "title": parsed.get("title") or ""},
                              error=walls.message(wall).removeprefix("Error: "))
            time.sleep(_WALL_RECHECK_S)
        return opened  # unreachable: the loop always returns

    def check_wall(self, requested_url: str = "") -> Result:
        """Probe the current page for a wall without navigating: ``kind="wall"`` with
        ``Result.wall``, or ok with ``value`` = the probe dict (url, title, text, …).
        ``requested_url``: what you meant to open, so a redirect onto a login page counts."""
        with self.hold():
            probe = self.eval_json(walls.PROBE_JS)
            if not probe.ok:
                return probe
            if (wall := walls.classify(probe.value, requested_url)) is not None:
                return Result(WALL, wall=wall, value=probe.value, error=walls.message(wall).removeprefix("Error: "))
            return Result(OK, value=probe.value, seconds=probe.seconds)

    def get_url(self) -> Result:
        """``text`` = the current page's URL."""
        with self.hold():
            r = self._command("get", "url", json_out=True)
            if r.ok:
                return Result(OK, text=str((r.value or {}).get("url") or ""), seconds=r.seconds)
            return r

    # ── waiting ──────────────────────────────────────────────────────────────────────────
    def wait(self, *, selector: str = "", text: str = "", url: str = "", load: str = "", js: str = "",
             timeout_s: float = 10.0) -> Result:
        """Wait until ONE condition holds: ``selector`` (CSS) visible, ``text`` on the page, the
        URL contains ``url`` (a plain substring), ``load`` state reached
        (load/domcontentloaded/networkidle), or JS ``js`` truthy. ``kind="timeout"`` when it
        didn't happen within ``timeout_s`` (default 10, max 25) — a legitimate answer, e.g. a
        results list that never appears because a wall did."""
        given = {k: v for k, v in (("selector", selector), ("text", text), ("url", url),
                                   ("load", load), ("js", js)) if str(v).strip()}
        if len(given) != 1:
            return Result(BAD_ARGUMENT, error=f"wait() takes exactly one condition (got {', '.join(given) or 'none'})")
        if (bad := self._refuse(**given)):
            return bad
        try:
            secs = float(timeout_s)
        except (TypeError, ValueError):
            return Result(BAD_ARGUMENT, error="timeout_s must be a number of seconds")
        # Below the CLI's 25 s action budget and the subprocess timeout, so the DAEMON reports its
        # own timeout (a killed CLI leaves the daemon still waiting).
        secs = max(0.1, min(secs, _WAIT_MAX_S, self.timeout_s - 2.0))
        ms = str(int(secs * 1000))
        if selector:
            if selector.strip().startswith("@") or selector.strip().isdigit():
                return Result(BAD_ARGUMENT, error="wait(selector=) takes a CSS selector (not a @ref or a number)")
            argv = ["wait", selector.strip()]
        elif text:
            argv = ["wait", "--text", text, "--timeout", ms]
        elif url:
            pat = url.strip().strip("*")
            if not pat or "*" in pat:
                return Result(BAD_ARGUMENT, error="wait(url=) is a plain substring of the page URL (no wildcards)")
            argv = ["wait", "--url", pat]
        elif load:
            if load.strip() not in _LOAD_STATES:
                return Result(BAD_ARGUMENT, error="wait(load=) must be load, domcontentloaded or networkidle")
            argv = ["wait", "--load", load.strip()]
        else:
            argv = ["wait", "--fn", js]
        with self.hold():
            # Every form but --text takes its timeout from AGENT_BROWSER_DEFAULT_TIMEOUT, which the
            # CLI injects into the command (commands.rs parse_command). That var also becomes the
            # default of a daemon the CLI SPAWNS, so the wait never runs on a cold session: it
            # follows an open, which is what started the daemon.
            r = self._command(*argv, json_out=True, env_extra={"AGENT_BROWSER_DEFAULT_TIMEOUT": ms},
                              timeout=secs + 5.0)
            if r.kind == TIMEOUT:
                what = next(iter(given))
                return Result(TIMEOUT, command="wait", seconds=r.seconds, error=(
                    f"waited {secs:g}s for {what}={given[what]!r} and it didn't happen"))
            return r

    # ── reading ──────────────────────────────────────────────────────────────────────────
    def eval_json(self, script: str) -> Result:
        """Run ``script`` (a JS expression) in the page; ``value`` = its result, parsed. The
        script goes on stdin, never argv (quotes, size). A script that returns
        ``JSON.stringify(x)`` gets ``x`` back. ``kind="script_error"`` when the script threw."""
        if not str(script or "").strip():
            return Result(BAD_ARGUMENT, error="eval_json() needs a script")
        with self.hold():
            r = self._command("eval", "--stdin", stdin=script, json_out=True)
            if r.ok:
                data = r.value if isinstance(r.value, dict) else {}
                return Result(OK, value=_decode_value(data.get("result")), seconds=r.seconds, command="eval")
            if r.kind == CLI_ERROR and "Evaluation error" in r.error:
                return Result(SCRIPT_ERROR, error=r.error, seconds=r.seconds, command="eval")
            return r

    def get_text(self, selector: str = "body", *, content_boundaries: bool = False) -> Result:
        """``text`` = the visible text of ``selector`` (CSS or ``@ref``). Cut at the byte cap with
        ``truncated=True`` rather than failing. ``content_boundaries`` wraps it in the CLI's
        untrusted-page-content markers — use it when the text goes to a model."""
        if (bad := self._refuse(selector=selector)):
            return bad
        pre = (boundaries.FLAG,) if content_boundaries else ()
        with self.hold():
            return self._command(*pre, "get", "text", selector.strip() or "body", truncatable=True)

    def snapshot(self, *, interactive: bool = True, compact: bool = True, scope: str = "",
                 depth: int = 0, content_boundaries: bool = False) -> Result:
        """``text`` = the accessibility tree with ``@eN`` refs (interactive + compact by default —
        5-8x smaller than the full tree). ``scope``: a CSS selector; ``depth``: max levels."""
        if (bad := self._refuse(scope=scope)):
            return bad
        argv = ["snapshot"] + (["-i"] if interactive else []) + (["-c"] if compact else [])
        if scope.strip():
            argv += ["-s", scope.strip()]
        if int(depth or 0) > 0:
            argv += ["-d", str(int(depth))]
        pre = (boundaries.FLAG,) if content_boundaries else ()
        with self.hold():
            return self._command(*pre, *argv, truncatable=True)

    def form_read(self, scope: str = "") -> Result:
        """Read every form field on the current page (or within ``scope``: a CSS selector or a
        visible container label, not a ``@ref``) with the SAME in-page reader as
        ``browser_form_read`` — including opening each unanswered select-like combobox to list
        its options, and closing it again without committing anything. ``value`` = the list of
        field dicts in document order, the shape the tool renders: ``label``, ``kind``, ``name``,
        ``id``, ``required``, ``value`` (the COMMITTED value; a list for a checkbox group), plus
        ``options`` / ``options_more`` where the field has them. ``[]`` when the page has no
        fields. Structured data for code — no content-boundary markers.

        ``kind="cli_error"`` when ``scope`` matched nothing; ``"script_error"`` when the page
        script threw or answered something unreadable; ``"too_large"`` past the byte cap."""
        scope = str(scope or "").strip()
        if scope and forms.is_ref(scope):
            return Result(BAD_ARGUMENT, command="form_read", error=(
                "form_read() can't scope by a @ref (the CLI resolves those, not the page) — pass a "
                "CSS selector or a visible container label, or leave scope blank."))
        # The scope rides inside the script (stdin), never argv, so it needs no option guard.
        with self.hold():
            r = self._command("eval", "--stdin", stdin=forms.read_form_js(scope), json_out=True)
        if not r.ok:
            if r.kind == CLI_ERROR and "Evaluation error" in r.error:
                return Result(SCRIPT_ERROR, error=r.error, seconds=r.seconds, command="form_read")
            return Result(r.kind, text=r.text, error=r.error, seconds=r.seconds, command="form_read")
        raw = (r.value or {}).get("result") if isinstance(r.value, dict) else None
        if isinstance(raw, str):
            raw = boundaries.strip(raw)  # never on here, but a stray env flag must not break the parse
        try:
            data = forms.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            data = None
        fields, error = forms.read_fields(data, scope) if data is not None else (
            None, "Error: could not read the form (the page returned unreadable data).")
        if error:
            kind = CLI_ERROR if "matched the scope" in error else SCRIPT_ERROR
            return Result(kind, error=error.removeprefix("Error: "), seconds=r.seconds, command="form_read")
        return Result(OK, value=fields, seconds=r.seconds, command="form_read")

    def close(self) -> Result:
        """Close this client's browser session (its daemon and Chrome). Only ever this session —
        and refused on the agent's session (:meth:`for_agent_session`): that browser isn't the
        plugin's to close."""
        if self.agent_session:
            return Result(BAD_ARGUMENT, command="close", error=(
                "close() is refused on the agent's own browser session — it belongs to the agent's "
                "browser tools (and maybe the operator's signed-in tabs), not to this plugin."))
        with self.hold():
            return self._command("close")


def _agent_session_name(scope: str, state: Any, session_id: str | None) -> tuple[str, bool]:
    """``(name, is_chat)`` — the session the ``browser_*`` tools resolve for this call. Mirrors
    ``tools._resolve_session_name`` (a test pins the two together): graph state is authoritative,
    an empty id there falls back to the shared session just as the tools do."""
    if scope == "per_session":
        if state is not None:
            sid = state.get("session_id", "") if hasattr(state, "get") else getattr(state, "session_id", "")
            sid = str(sid or "")
        else:
            sid = str(session_id or "") or _tracing_session_id()
            if not sid:
                raise ValueError(
                    "for_agent_session(): agent_browser runs session_scope=per_session, so the "
                    "agent's browser is per chat — pass the calling tool's injected `state` (or "
                    "`session_id=`); the tracing id is empty inside a tool body.")
        if sid:
            return runtime.chat_session_name(sid), True
    name = runtime.shared_session_name()
    if runtime.OPTION_LIKE.match(name):
        raise ValueError(f"AGENT_BROWSER_SESSION {name!r} looks like a command-line option")
    return name, False


def _tracing_session_id() -> str:
    try:
        from observability import tracing
        return tracing.current_session_id() or ""
    except Exception:  # noqa: BLE001 — a telemetry read must never break a browser call
        return ""


def make_factory(live_cfg, refresh_gaps=None):
    """The ``agent_browser.client`` service: ``make(**options) -> BrowserClient``.

    ``live_cfg`` returns the agent_browser plugin's CURRENT config: the CLI ``binary``,
    ``cli_autofetch`` and ``max_response_bytes`` are core's (one CLI, one byte cap for every
    caller), read when each command runs, so a Settings change applies to clients already
    built. Everything about the session itself is the calling plugin's (see BrowserClient)."""

    host = _Host(live_cfg, refresh_gaps)

    def make(**options) -> BrowserClient:
        """Build a :class:`BrowserClient` for the calling plugin — see its docstring for the
        options and ``plugins/agent_browser/client.py`` for the full contract. Raises
        ``ValueError`` for an invalid option (a programming error, not a runtime "no")."""
        options.pop("host", None)
        return BrowserClient(host=host, **options)

    def for_agent_session(state: Any = None, *, session_id: str | None = None,
                          timeout_s: float = 30.0, nav_timeout_s: float = 45.0) -> BrowserClient:
        """A client on the agent's own browser session for the calling chat (pass the calling
        tool's injected ``state``) — see ``BrowserClient.for_agent_session``."""
        return BrowserClient.for_agent_session(state, session_id=session_id, timeout_s=timeout_s,
                                               nav_timeout_s=nav_timeout_s, host=host)

    make.for_agent_session = for_agent_session
    make.BrowserClient = BrowserClient  # introspection: help(make.BrowserClient)
    make.KINDS = KINDS
    return make
