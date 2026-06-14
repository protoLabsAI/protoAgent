"""Delegate type adapters — a2a / openai / acp (ADR 0025).

Each adapter knows one delegate *type*: the fields it needs (a schema that drives
both the panel form and server-side validation), how to parse a raw config dict
into a ``Delegate``, and how to ``dispatch`` a query to it. A reachability
``probe`` (the panel's "Test" button) lands with the REST API in PR2.

Ported/unified from ORBIS's ``agent/delegate_adapters.py`` — the canonical
protoLabs delegate registry — adapted to protoAgent (the acp adapter reuses the
ADR 0024 ``AcpClient``; the a2a adapter reuses the ``peer_tools`` A2A parse helpers).
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
    key: str                 # dotted config key, e.g. "auth.token"
    label: str
    kind: str = "text"       # text | secret | args | path | number | textarea | select
    required: bool = False
    help: str = ""
    placeholder: str = ""
    options: list[str] = field(default_factory=list)   # for kind=select
    default: object = None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "kind": self.kind,
            "required": self.required, "help": self.help,
            "placeholder": self.placeholder, "options": self.options, "default": self.default,
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
    auth_scheme: str = ""           # "" | bearer | apiKey
    auth_token: str = ""            # secret value (from secrets.yaml overlay)

    # openai
    model: str = ""
    api_key: str = ""               # secret value
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
        return {"name": name, "type": str(raw.get("type", "")).strip(),
                "description": str(raw.get("description", "")).strip()}


async def _timed(coro) -> tuple[object, int]:
    """Await ``coro``, returning (result, elapsed_ms)."""
    import time
    t0 = time.monotonic()
    res = await coro
    return res, int((time.monotonic() - t0) * 1000)


class A2aAdapter(Adapter):
    type = "a2a"
    label = "A2A agent"
    blurb = "A fleet peer over the A2A JSON-RPC protocol."
    secret_field = "auth.token"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec("url", "URL", "text", required=True,
                      placeholder="https://peer.example/a2a",
                      help="The peer's A2A endpoint (usually ends in /a2a)."),
            FieldSpec("auth.scheme", "Auth scheme", "select", options=["", "bearer", "apiKey"],
                      help="How the peer expects credentials, if any."),
            FieldSpec("auth.token", "Auth token", "secret",
                      help="Stored in secrets.yaml (gitignored), never in tracked config."),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.url = str(raw.get("url", "")).strip()
        if not d.url:
            raise DelegateError(f"a2a delegate {d.name!r} needs a url")
        auth = raw.get("auth") or {}
        d.auth_scheme = str(auth.get("scheme", "")).strip()
        d.auth_token = _secret(auth, "token", "credentialsEnv")
        return d

    async def dispatch(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        import httpx

        from security import policy
        from tools.peer_tools import _extract_text, _is_terminal

        blocked = policy.check_url(d.url)
        if blocked:
            raise DelegateError(blocked.replace("destination", f"delegate {d.name!r}", 1))
        # A2A-Version is mandatory for an a2a-sdk >=1.0 peer: a missing header defaults
        # to 0.3 on the receiver → -32009 VERSION_NOT_SUPPORTED (ADR 0051 audit). The
        # scheduler/inbox/background self-POSTs already set it; the delegate client must too.
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if d.auth_token:
            headers["Authorization"] = (
                f"Bearer {d.auth_token}" if d.auth_scheme != "apiKey" else d.auth_token
            )
            if d.auth_scheme == "apiKey":
                headers["X-API-Key"] = d.auth_token

        async def _rpc(client, method, params):
            body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
            r = await client.post(d.url, json=body, headers=headers)
            if r.status_code >= 400:
                raise DelegateError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if data.get("error"):
                raise DelegateError(str(data["error"]))
            return data.get("result") or {}

        # A2A 1.0 (a2a-sdk >=1.0): JSON-RPC `SendMessage` / `GetTask`, the ROLE_USER
        # enum, and a `result.task` envelope. (`message/send` + lowercase `user` is
        # the v0.3 legacy dialect, which 1.0 servers reject with -32601.)
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            result = await _rpc(client, "SendMessage", {"message": {
                "role": "ROLE_USER", "parts": [{"text": query}],
                "messageId": str(uuid.uuid4()),
            }})
            text = _extract_text(result)
            if text:
                return text
            task = result.get("task", result) or {}
            task_id = task.get("id")
            state = (task.get("status") or {}).get("state")
            polls = 0
            while task_id and not _is_terminal(state) and polls < 30:
                await asyncio.sleep(1.0)
                polls += 1
                result = await _rpc(client, "GetTask", {"name": task_id})
                task = result.get("task", result) or {}
                state = (task.get("status") or {}).get("state")
            text = _extract_text(result)
            if text:
                return text
            raise DelegateError(f"no text returned (state={state})")

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
            name = (r.json() or {}).get("name", "")
            return {"ok": True, "latency_ms": ms, "detail": f"agent-card OK ({name})"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}


class OpenAiAdapter(Adapter):
    type = "openai"
    label = "Model endpoint"
    blurb = "An OpenAI-compatible chat endpoint — ask another model."
    secret_field = "api_key"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec("url", "Base URL", "text", required=True,
                      placeholder="https://api.proto-labs.ai/v1",
                      help="OpenAI-compatible base URL (the /chat/completions parent)."),
            FieldSpec("model", "Model", "text", required=True,
                      placeholder="protolabs/reasoning"),
            FieldSpec("api_key", "API key", "secret",
                      help="Stored in secrets.yaml (gitignored)."),
            FieldSpec("system_prompt", "System prompt", "textarea",
                      placeholder="Answer thoroughly but concisely."),
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
        payload = {"model": d.model, "messages": messages,
                   "max_tokens": d.max_tokens, "temperature": d.temperature}
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
            FieldSpec("command", "Command", "text", required=True, placeholder="proto",
                      help="Binary on PATH that speaks ACP."),
            FieldSpec("args", "Args", "args", placeholder="--acp",
                      help="Launch args, e.g. --acp."),
            FieldSpec("workdir", "Workdir", "path", required=True, placeholder="~/dev/my-repo",
                      help="Session cwd — the confinement boundary."),
            FieldSpec("permissions", "Permissions", "select",
                      options=["auto", "allowlist", "readonly"], default="auto",
                      help="By-kind permission policy for the agent's actions."),
            FieldSpec("confirm", "Confirm each call", "select", options=["false", "true"],
                      default="false", help="Ask the operator before each call."),
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
            "name": d.name, "command": d.command, "args": d.args, "workdir": d.workdir,
            "env": d.env or None, "permissions": d.permissions,
            "allow_kinds": d.allow_kinds, "deny_kinds": d.deny_kinds,
        }

    async def dispatch(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        # Reuse the ADR 0024 ACP client + by-kind permission policy.
        from plugins.coding_agent import _client_for, _make_permission
        from plugins.coding_agent.acp_client import AcpError

        spec = self._spec(d)
        client = _client_for(spec)
        client._permission = _make_permission(spec)
        try:
            return await client.prompt(query, timeout=timeout or d.timeout_s)
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

    async def probe(self, d: Delegate) -> dict:
        import os
        import shutil
        if not shutil.which(d.command):
            return {"ok": False, "error": f"binary not on PATH: {d.command!r}"}
        wd = os.path.expanduser(d.workdir)
        if not os.path.isdir(wd):
            return {"ok": False, "error": f"workdir does not exist: {wd}"}
        return {"ok": True, "detail": f"{d.command} on PATH; workdir OK"}


ADAPTERS: dict[str, Adapter] = {a.type: a for a in (A2aAdapter(), OpenAiAdapter(), AcpAdapter())}


def delegate_types() -> list[dict]:
    """Type list + field schemas — drives the panel (PR3) and /delegate-types (PR2)."""
    return [
        {"type": a.type, "label": a.label, "blurb": a.blurb,
         "fields": [f.as_dict() for f in a.config_schema()]}
        for a in ADAPTERS.values()
    ]
