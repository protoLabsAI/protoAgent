"""Delegate type adapters — a2a / openai / acp (ADR 0025).

Each adapter knows one delegate *type*: the fields it needs (a schema that drives
both the panel form and server-side validation), how to parse a raw config dict
into a ``Delegate``, and how to ``dispatch`` a query to it. A reachability
``probe`` (the panel's "Test" button) lands with the REST API in PR2.

Ported/unified from ORBIS's ``agent/delegate_adapters.py`` — the canonical
protoLabs delegate registry — adapted to protoAgent (the acp adapter reuses the
ADR 0024 ``AcpClient``; the a2a adapter reuses the ``a2a_parse`` A2A parse helpers).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("protoagent.plugins.delegates")


class DelegateError(Exception):
    """A dispatch/parse failure. The caller turns it into a tool error string."""


# ── field schema (drives the panel form + validation) ─────────────────────────


@dataclass
class FieldSpec:
    key: str  # dotted config key, e.g. "auth.token"
    label: str
    kind: str = "text"  # text | secret | args | path | number | textarea | select
    required: bool = False
    help: str = ""
    placeholder: str = ""
    options: list[str] = field(default_factory=list)  # for kind=select
    default: object = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "help": self.help,
            "placeholder": self.placeholder,
            "options": self.options,
            "default": self.default,
        }


# ── the unified delegate model ────────────────────────────────────────────────


@dataclass
class Delegate:
    """One dispatch target, switched on ``type``."""

    name: str
    type: str
    description: str = ""

    # a2a
    url: str = ""
    auth_scheme: str = ""  # "" | bearer | apiKey
    auth_token: str = ""  # secret value (from secrets.yaml overlay)
    poll_timeout_s: float = 300.0  # a2a: max seconds to await a long-running delegated task

    # openai
    model: str = ""
    api_key: str = ""  # secret value
    system_prompt: str = ""
    max_tokens: int = 1024
    temperature: float = 0.4

    # acp
    command: str = ""
    args: list[str] = field(default_factory=list)
    workdir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 600.0
    permissions: str = "auto"
    allow_kinds: list[str] = field(default_factory=list)
    deny_kinds: list[str] = field(default_factory=list)
    confirm: bool = False


def _secret(raw: dict, value_key: str, env_key: str) -> str:
    """Resolve a secret: explicit value (from the secrets.yaml overlay) wins;
    else read the named env var (``<field>_env``) if given. Never logs the value."""
    val = str(raw.get(value_key) or "").strip()
    if val:
        return val
    env_name = str(raw.get(env_key) or "").strip()
    return os.environ.get(env_name, "") if env_name else ""


# ── adapters ──────────────────────────────────────────────────────────────────


class Adapter:
    """Base class. Subclasses set ``type`` and implement schema/parse/dispatch."""

    type: str = ""
    label: str = ""
    blurb: str = ""

    def config_schema(self) -> list[FieldSpec]:
        raise NotImplementedError

    def parse(self, raw: dict) -> Delegate:
        raise NotImplementedError

    async def dispatch(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        raise NotImplementedError

    async def probe(self, d: Delegate) -> dict:
        """Reachability check for the panel's Test button: {ok, latency_ms, error}."""
        return {"ok": None, "error": "probe not implemented for this type"}

    # secret field this type stores (for the CRUD secret overlay), as a dotted
    # path into the raw entry. None ⇒ no secret.
    secret_field: str | None = None

    # Shared helpers ---------------------------------------------------------
    @staticmethod
    def _base(raw: dict) -> dict:
        name = str(raw.get("name", "")).strip()
        if not name:
            raise DelegateError("delegate needs a name")
        return {
            "name": name,
            "type": str(raw.get("type", "")).strip(),
            "description": str(raw.get("description", "")).strip(),
        }


async def _timed(coro) -> tuple[object, int]:
    """Await ``coro``, returning (result, elapsed_ms)."""
    import time

    t0 = time.monotonic()
    res = await coro
    return res, int((time.monotonic() - t0) * 1000)


def _a2a_error_detail(d: Delegate, err: object) -> str:
    """Turn a JSON-RPC error payload into an operator-legible cause — especially the
    version-skew case, which otherwise surfaces as an opaque ``-32009``."""
    code = err.get("code") if isinstance(err, dict) else None
    msg = str(err.get("message")) if isinstance(err, dict) else str(err)
    if code == -32009 or "VERSION_NOT_SUPPORTED" in str(err).upper():
        return (
            f"delegate {d.name!r}: peer rejected A2A-Version 1.0 (VERSION_NOT_SUPPORTED) — it speaks "
            "an older A2A dialect. Upgrade the peer, or point its url at a 1.0 /a2a endpoint."
        )
    return f"delegate {d.name!r}: {msg or err}"


# The A2A protocol version(s) our delegate client can speak (it sends the
# ``A2A-Version: 1.0`` header + the 1.0 SendMessage/GetTask dialect). Used to
# pre-check a peer's advertised version and fail fast on a clear mismatch.
_A2A_SUPPORTED_VERSIONS = ("1.0",)


def _advertised_a2a_versions(card: dict) -> list[str]:
    """Every A2A protocol version a peer's agent-card advertises (de-duped, in
    first-seen order), or ``[]`` if the card says nothing about it.

    Reads the native proto field (``supportedInterfaces[].protocolVersion``) AND
    the proto-free top-level hint (``protocolVersion`` / ``supportedVersions``)
    that protoLabs agents also expose — so an older or non-protoLabs peer is still
    understood when it advertises its version in either shape. ``[]`` means
    *don't know* (older peers, partial cards): callers must treat that as
    best-effort and NOT block."""
    if not isinstance(card, dict):
        return []
    seen: list[str] = []

    def _add(v: object) -> None:
        s = str(v or "").strip()
        if s and s not in seen:
            seen.append(s)

    for iface in card.get("supportedInterfaces") or []:
        if isinstance(iface, dict):
            _add(iface.get("protocolVersion"))
    _add(card.get("protocolVersion"))
    versions = card.get("supportedVersions")
    if isinstance(versions, (list, tuple)):
        for v in versions:
            _add(v)
    return seen


class A2aAdapter(Adapter):
    type = "a2a"
    label = "A2A agent"
    blurb = "A fleet peer over the A2A JSON-RPC protocol."
    secret_field = "auth.token"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                "URL",
                "text",
                required=True,
                placeholder="https://peer.example/a2a",
                help="The peer's A2A endpoint (usually ends in /a2a).",
            ),
            FieldSpec(
                "auth.scheme",
                "Auth scheme",
                "select",
                options=["", "bearer", "apiKey"],
                help="How the peer expects credentials, if any.",
            ),
            FieldSpec(
                "auth.token",
                "Auth token",
                "secret",
                help="Stored in secrets.yaml (gitignored), never in tracked config.",
            ),
            FieldSpec(
                "poll_timeout_s",
                "Task poll timeout (s)",
                "number",
                default=300,
                help="Max seconds to wait for a long-running delegated task to finish before "
                "giving up locally — the peer keeps working. Raise it for slow agents (e.g. a "
                "code build); the old fixed 30s cut long tasks off mid-flight.",
            ),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.url = str(raw.get("url", "")).strip()
        if not d.url:
            raise DelegateError(f"a2a delegate {d.name!r} needs a url")
        auth = raw.get("auth") or {}
        d.auth_scheme = str(auth.get("scheme", "")).strip()
        d.auth_token = _secret(auth, "token", "credentialsEnv")
        try:
            d.poll_timeout_s = float(raw.get("poll_timeout_s") or 300.0)
        except (TypeError, ValueError):
            d.poll_timeout_s = 300.0
        return d

    async def dispatch(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        import time

        import httpx

        from security import policy
        from tools.a2a_parse import _extract_text, _is_terminal

        blocked = policy.check_url(d.url)
        if blocked:
            raise DelegateError(blocked.replace("destination", f"delegate {d.name!r}", 1))
        # Pre-flight protocol-version check (best-effort). Fetch the peer's card and, if
        # it CLEARLY advertises an A2A protocol version we can't speak, fail fast with a
        # legible mismatch instead of sending and waiting for the opaque -32009
        # VERSION_NOT_SUPPORTED mid-dispatch. A silent/unreachable card never blocks — we
        # fall through to dispatch, whose -32009 mapping (``_a2a_error_detail``) still applies.
        info = await self.probe(d)
        advertised = info.get("supported_versions") or []
        if advertised and not any(v in _A2A_SUPPORTED_VERSIONS for v in advertised):
            raise DelegateError(
                f"delegate {d.name!r} advertises A2A protocol {'/'.join(advertised)} but this agent "
                f"speaks {'/'.join(_A2A_SUPPORTED_VERSIONS)} — refusing to dispatch (an older peer "
                "would reject the call with -32009 VERSION_NOT_SUPPORTED). Upgrade the peer, or point "
                "its url at a 1.0 /a2a endpoint."
            )
        # A2A-Version is mandatory for an a2a-sdk >=1.0 peer: a missing header defaults
        # to 0.3 on the receiver → -32009 VERSION_NOT_SUPPORTED (ADR 0051 audit). The
        # scheduler/inbox/background self-POSTs already set it; the delegate client must too.
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if d.auth_token:
            headers["Authorization"] = f"Bearer {d.auth_token}" if d.auth_scheme != "apiKey" else d.auth_token
            if d.auth_scheme == "apiKey":
                headers["X-API-Key"] = d.auth_token

        async def _rpc(client, method, params):
            body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
            # Map transport failures to a legible CAUSE — a delegating agent (and the
            # operator) needs "unreachable" vs "timed out" vs "version-incompatible",
            # not an opaque stack trace or a bare connection error.
            try:
                r = await client.post(d.url, json=body, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise DelegateError(f"delegate {d.name!r} unreachable at {d.url} ({type(exc).__name__})") from exc
            except httpx.TimeoutException as exc:
                raise DelegateError(f"delegate {d.name!r} timed out contacting {d.url}") from exc
            except httpx.HTTPError as exc:
                raise DelegateError(f"delegate {d.name!r} transport error: {str(exc)[:160]}") from exc
            if r.status_code >= 400:
                raise DelegateError(f"delegate {d.name!r} HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if data.get("error"):
                raise DelegateError(_a2a_error_detail(d, data["error"]))
            return data.get("result") or {}

        poll_timeout = d.poll_timeout_s if d.poll_timeout_s and d.poll_timeout_s > 0 else 300.0
        # A2A 1.0 (a2a-sdk >=1.0): JSON-RPC `SendMessage` / `GetTask`, the ROLE_USER
        # enum, and a `result.task` envelope. (`message/send` + lowercase `user` is
        # the v0.3 legacy dialect, which 1.0 servers reject with -32601.) The per-request
        # client timeout caps a single call; the poll DEADLINE caps the overall wait for a
        # long-running task — so a 2-minute delegated task no longer fails at the old 30s.
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            result = await _rpc(
                client,
                "SendMessage",
                {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": query}],
                        "messageId": str(uuid.uuid4()),
                    }
                },
            )
            text = _extract_text(result)
            if text:
                return text
            task = result.get("task", result) or {}
            task_id = task.get("id")
            state = (task.get("status") or {}).get("state")
            deadline = time.monotonic() + poll_timeout
            while task_id and not _is_terminal(state) and time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                result = await _rpc(client, "GetTask", {"name": task_id})
                task = result.get("task", result) or {}
                state = (task.get("status") or {}).get("state")
            text = _extract_text(result)
            if text:
                return text
            if task_id and not _is_terminal(state):
                raise DelegateError(
                    f"delegate {d.name!r} still running after {int(poll_timeout)}s — the peer may "
                    f"still be working; raise its poll timeout if tasks legitimately take longer "
                    f"(state={state})"
                )
            raise DelegateError(f"delegate {d.name!r} returned no text (state={state})")

    async def probe(self, d: Delegate) -> dict:
        import httpx

        from security import policy

        origin = d.url.split("/a2a")[0].rstrip("/") if "/a2a" in d.url else d.url.rstrip("/")
        card = f"{origin}/.well-known/agent-card.json"
        blocked = policy.check_url(card)
        if blocked:
            return {"ok": False, "error": blocked}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r, ms = await _timed(client.get(card))
            if r.status_code >= 400:
                return {"ok": False, "latency_ms": ms, "error": f"HTTP {r.status_code}"}
            body = r.json() or {}
            name = body.get("name", "")
            # Capture the peer's advertised A2A protocol version(s) so a caller (and
            # dispatch's pre-check) can fail fast on a version mismatch. ``version`` is
            # the peer's APP version (distinct); ``protocol_version`` is the primary
            # advertised protocol, "" when the card is silent (older peers).
            advertised = _advertised_a2a_versions(body)
            pv = advertised[0] if advertised else ""
            detail = f"agent-card OK ({name})" + (f", A2A {pv}" if pv else "")
            return {
                "ok": True,
                "latency_ms": ms,
                "protocol_version": pv,
                "supported_versions": advertised,
                "version": body.get("version", ""),
                "detail": detail,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}


class OpenAiAdapter(Adapter):
    type = "openai"
    label = "Model endpoint"
    blurb = "An OpenAI-compatible chat endpoint — ask another model."
    secret_field = "api_key"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                "Base URL",
                "text",
                required=True,
                placeholder="https://api.proto-labs.ai/v1",
                help="OpenAI-compatible base URL (the /chat/completions parent).",
            ),
            FieldSpec("model", "Model", "text", required=True, placeholder="protolabs/reasoning"),
            FieldSpec("api_key", "API key", "secret", help="Stored in secrets.yaml (gitignored)."),
            FieldSpec("system_prompt", "System prompt", "textarea", placeholder="Answer thoroughly but concisely."),
            FieldSpec("max_tokens", "Max tokens", "number", default=1024),
            FieldSpec("temperature", "Temperature", "number", default=0.4),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.url = str(raw.get("url", "")).strip()
        d.model = str(raw.get("model", "")).strip()
        if not (d.url and d.model):
            raise DelegateError(f"openai delegate {d.name!r} needs url + model")
        d.api_key = _secret(raw, "api_key", "api_key_env")
        d.system_prompt = str(raw.get("system_prompt", "")).strip()
        try:
            d.max_tokens = int(raw.get("max_tokens") or 1024)
        except (TypeError, ValueError):
            d.max_tokens = 1024
        try:
            d.temperature = float(raw.get("temperature") if raw.get("temperature") is not None else 0.4)
        except (TypeError, ValueError):
            d.temperature = 0.4
        return d

    async def dispatch(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        import httpx

        messages = []
        if d.system_prompt:
            messages.append({"role": "system", "content": d.system_prompt})
        messages.append({"role": "user", "content": query})
        headers = {"Content-Type": "application/json"}
        if d.api_key:
            headers["Authorization"] = f"Bearer {d.api_key}"
        url = d.url.rstrip("/") + "/chat/completions"
        payload = {"model": d.model, "messages": messages, "max_tokens": d.max_tokens, "temperature": d.temperature}
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                raise DelegateError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise DelegateError(f"unexpected response shape: {exc}")

    async def probe(self, d: Delegate) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {d.api_key}"} if d.api_key else {}
        url = d.url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r, ms = await _timed(client.get(url, headers=headers))
            if r.status_code >= 400:
                return {"ok": False, "latency_ms": ms, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
            return {"ok": True, "latency_ms": ms, "detail": "endpoint reachable"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}


class AcpAdapter(Adapter):
    type = "acp"
    label = "Coding agent (ACP)"
    blurb = "A CLI coding agent (protoCLI, Claude Code, …) driven over ACP."

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "command",
                "Command",
                "text",
                required=True,
                placeholder="proto",
                help="Binary on PATH that speaks ACP (e.g. proto). For Claude Code use `claude-code` — an alias for the claude-agent-acp adapter.",
            ),
            FieldSpec(
                "args",
                "Args",
                "args",
                placeholder="--acp",
                help="Launch args (e.g. --acp). Leave empty for claude-code.",
            ),
            FieldSpec(
                "workdir",
                "Workdir",
                "path",
                required=True,
                placeholder="~/dev/my-repo",
                help="Session cwd — the confinement boundary.",
            ),
            FieldSpec(
                "permissions",
                "Permissions",
                "select",
                options=["auto", "allowlist", "readonly"],
                default="auto",
                help="By-kind permission policy for the agent's actions.",
            ),
            FieldSpec(
                "confirm",
                "Confirm each call",
                "select",
                options=["false", "true"],
                default="false",
                help="Ask the operator before each call.",
            ),
            FieldSpec("timeout_s", "Timeout (s)", "number", default=600),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.command = str(raw.get("command", "")).strip()
        d.workdir = str(raw.get("workdir", "")).strip()
        if not (d.command and d.workdir):
            raise DelegateError(f"acp delegate {d.name!r} needs command + workdir")
        args = raw.get("args") or []
        d.args = [str(a) for a in args] if isinstance(args, (list, tuple)) else []
        # Convenience alias (issue #1116): Claude Code has NO native ACP mode — it needs
        # the claude-agent-acp adapter (formerly @zed-industries/claude-code-acp, now
        # deprecated). Let operators write the intuitive `command: claude-code` and map
        # it to the adapter binary, which takes no launch args — instead of hand-authoring
        # the incantation and getting an opaque `agent exited` at first dispatch.
        if d.command in ("claude-code", "claude-acp"):
            d.command = "claude-agent-acp"
            d.args = []
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        d.env = {str(k): str(v) for k, v in env.items()}
        try:
            d.timeout_s = float(raw.get("timeout_s") or 600)
        except (TypeError, ValueError):
            d.timeout_s = 600.0
        d.permissions = str(raw.get("permissions", "auto")).strip().lower() or "auto"
        d.allow_kinds = [str(k).lower() for k in (raw.get("allow_kinds") or [])]
        d.deny_kinds = [str(k).lower() for k in (raw.get("deny_kinds") or [])]
        d.confirm = str(raw.get("confirm", "")).strip().lower() in ("1", "true", "yes")
        return d

    @staticmethod
    def _spec(d: Delegate) -> dict:
        """The coding_agent spec dict for this delegate. Shared by ``dispatch`` and
        ``teardown`` so both compute the SAME client cache key (which includes
        ``workdir``) — so a caller that scopes ``workdir`` per call (e.g. via
        ``dataclasses.replace`` onto a disposable worktree) tears down the exact
        client it dispatched."""
        return {
            "name": d.name,
            "command": d.command,
            "args": d.args,
            "workdir": d.workdir,
            "env": d.env or None,
            "permissions": d.permissions,
            "allow_kinds": d.allow_kinds,
            "deny_kinds": d.deny_kinds,
        }

    async def dispatch(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        # Reuse the ADR 0024 ACP client + by-kind permission policy.
        from plugins.coding_agent import _client_for, _drop_client, _make_permission
        from plugins.coding_agent.acp_client import AcpError

        spec = self._spec(d)
        client = _client_for(spec)
        client._permission = _make_permission(spec)
        try:
            return await client.prompt(query, timeout=timeout or d.timeout_s)
        except asyncio.CancelledError:
            # The turn was stopped (operator hit stop, or an orchestrator watchdog
            # fired). The client is POOLED, so without this its subprocess keeps
            # running detached — exactly "I stopped the main thread and the delegate
            # didn't stop". Drop it from the pool + SIGKILL the agent tree NOW
            # (synchronous, no awaits — we're mid-cancellation) before re-raising.
            _drop_client(spec)
            client.kill_now()
            raise
        except AcpError as exc:
            raise DelegateError(str(exc))

    async def teardown(self, d: Delegate) -> bool:
        """Evict + terminate the cached ACP subprocess for this delegate.

        ``dispatch`` caches a long-lived ``AcpClient`` (subprocess + session) keyed
        partly on ``workdir``. A caller that dispatches into a transient, per-call
        ``workdir`` should call this when done (e.g. in a ``finally``) so the child
        is reaped rather than left running — a plain cache ``pop`` forgets the
        handle but leaves the process alive. Returns True if a live client was
        closed; no-op (False) if none was started. Idempotent."""
        from plugins.coding_agent import evict_client

        return await evict_client(self._spec(d))

    async def forget_session(self, d: Delegate) -> bool:
        """Forget this delegate's persisted ACP session so the next ``dispatch``
        starts a fresh ``session/new`` (vs reattaching the prior thread). For a
        caller that recreates ``workdir`` fresh per call (a disposable worktree), a
        resumed session's memory would reference a diff the wiped tree no longer has.
        See ``coding_agent.forget_session``. Idempotent."""
        from plugins.coding_agent import forget_session

        return await forget_session(self._spec(d))

    async def probe(self, d: Delegate) -> dict:
        """Reachability check for the panel's Test button (also the periodic health
        prober's per-delegate check).

        Does a REAL ACP `initialize` handshake (spawn the command, complete the
        protocol handshake, close) — and NOTHING more: no `session/new`, no
        `session/load`, no `session/prompt`. So a launch command that's on PATH and
        workdir-valid but doesn't actually speak ACP FAILS the probe instead of
        showing green (issue #1116 — the old PATH+workdir check passed `command:
        claude` while every dispatch failed with an opaque `agent exited`), while a
        probe stays genuinely cheap + side-effect-free: it never opens a session every
        120s the way the old `_ensure_started` path did (#1300).
        """
        import asyncio
        import os
        import shutil

        # Bare `claude` is on PATH but has no native ACP mode — the classic false-green.
        # Steer to the adapter instead of spawning it only to watch the handshake hang.
        if os.path.basename(d.command) == "claude":
            return {
                "ok": False,
                "error": (
                    "`claude` has no native ACP mode. Claude Code needs the claude-agent-acp "
                    "adapter — `npm i -g @agentclientprotocol/claude-agent-acp`, then set "
                    "command: claude-code (an alias) or claude-agent-acp."
                ),
            }
        # Resolve the command against the SAME PATH the real spawn will use — the
        # delegate's env PATH overlaid on the process PATH — not just os.environ.
        # The actual ACP launch merges d.env (acp_client `_launch_env`/`env=…`), so a
        # delegate that supplies its own PATH (or runs under the desktop app's
        # augmented PATH) would spawn fine, yet the probe's bare `shutil.which` still
        # red-X'd it. Probe and spawn now agree on where to look (#1299).
        merged_path = (d.env or {}).get("PATH") or os.environ.get("PATH")
        if not shutil.which(d.command, path=merged_path):
            if os.path.basename(d.command) == "claude-agent-acp":
                return {
                    "ok": False,
                    "error": "claude-agent-acp not installed — run `npm i -g @agentclientprotocol/claude-agent-acp`.",
                }
            return {"ok": False, "error": f"binary not on PATH: {d.command!r}"}
        wd = os.path.expanduser(d.workdir)
        if not os.path.isdir(wd):
            return {"ok": False, "error": f"workdir does not exist: {wd}"}

        # Real handshake — `handshake()` spawns the agent and runs ACP `initialize`
        # ONLY (no session/new, no session/load), so it's a cheap, genuinely
        # side-effect-free liveness check (#1300).
        from plugins.coding_agent.acp_client import AcpClient

        client = AcpClient(command=d.command, args=d.args, cwd=wd, env=(d.env or None), name=d.name)
        try:
            await asyncio.wait_for(client.handshake(), timeout=45)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"ACP handshake timed out — does {d.command!r} speak ACP?"}
        except Exception as exc:  # noqa: BLE001 — spawn/handshake failure → tool-visible string
            return {"ok": False, "error": f"ACP handshake failed: {type(exc).__name__}: {exc}"}
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        return {"ok": True, "detail": f"ACP handshake OK (protocol {client._protocol_version}) — {d.command}"}


ADAPTERS: dict[str, Adapter] = {a.type: a for a in (A2aAdapter(), OpenAiAdapter(), AcpAdapter())}


def delegate_types() -> list[dict]:
    """Type list + field schemas — drives the panel (PR3) and /delegate-types (PR2)."""
    return [
        {"type": a.type, "label": a.label, "blurb": a.blurb, "fields": [f.as_dict() for f in a.config_schema()]}
        for a in ADAPTERS.values()
    ]
