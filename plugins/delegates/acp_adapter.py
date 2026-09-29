"""The ACP (coding agent) delegate adapter (ADR 0024/0025).

Split out of ``adapters.py`` (#3831). ``adapters`` re-exports ``AcpAdapter``,
``_mark_incomplete`` and ``_INCOMPLETE_STOP_REASONS`` and holds the ``ADAPTERS`` registry.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from .base import (
    Adapter,
    Delegate,
    DelegateError,
    FieldSpec,
    _env_fields,
    _parse_env,
)

if TYPE_CHECKING:
    from plugins.coding_agent.acp_client import ProgressCallback, TappedResult, ToolCallback


#: ACP ``stopReason`` values that mean the reply is CUT OFF rather than finished, mapped
#: to what the caller should do about it. ``prompt()`` returns text either way, so without
#: this an interrupted reply is indistinguishable from a complete one — the orchestrator
#: acts on a half-answer and the operator only finds out downstream (#2352).
#:
#: Its sibling is ``acp_client._UNFINISHED_STOP_REASONS``, which decides whether the run
#: RECORDS as a success. A reason marked incomplete here must be a failure there, or the
#: telemetry rollup books a success for a reply this module is telling the model not to
#: trust — pinned by a test, since the two lists live a package apart (#3015).
_INCOMPLETE_STOP_REASONS = {
    "max_tokens": (
        "the coding agent hit its output-token limit mid-generation, so this reply is CUT OFF "
        "and its final section may be missing or half-written. Do not treat it as complete: "
        "either re-dispatch the remaining work as a narrower follow-up query, or ask the "
        "delegate to continue from where it stopped."
    ),
    "refusal": (
        "the coding agent declined to finish this request, so the reply is incomplete. "
        "Re-dispatching the same query verbatim will decline again — restate the task or "
        "pick a different delegate."
    ),
}


def _mark_incomplete(reply: str, stop_reason: str | None) -> str:
    """Append an operator/model-visible marker when the coder's turn ended for a reason
    that means the text is truncated rather than finished.

    ``AcpClient`` records ``last_stop_reason`` on every turn and had no production reader
    at all — the signal existed and was dropped on the floor, which is exactly how a
    ``max_tokens`` cut-off reached the orchestrator looking like a finished answer. Kept
    OUT of ``prompt()`` itself: the coder ladder consumes replies as data and reads the
    stop reason directly via ``dead_end()``; it's the *delegate* surface, whose reply goes
    into a model's context as prose, that needs it spelled out.

    ``cancelled`` is deliberately absent — an operator stopping a turn already knows.

    Read immediately after the caller's own ``await client.prompt(...)`` returns. That is
    exactly as reliable as the reply text itself: both are per-client state the same call
    just wrote, on a client the pool already treats as single-flight (``prompt`` clears
    ``_answer`` on entry, so a concurrent same-delegate prompt would corrupt the reply
    long before it could confuse the stop reason).
    """
    reason = (stop_reason or "").strip()
    note = _INCOMPLETE_STOP_REASONS.get(reason)
    if not note:
        return reply
    return f"{reply}\n\n[incomplete reply — {note}]" if reply.strip() else f"[no reply — {note}]"


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
                advanced=True,
                help="Ask the operator before each call.",
            ),
            FieldSpec(
                "timeout_s",
                "Timeout (s)",
                "number",
                default=1800,
                advanced=True,
                help="Max seconds to await the coder's reply. Default 1800 (30 min) — "
                "implementation tasks (TDD cycles, venv setup, CI gates) can run long. "
                "delegate_to(timeout=…) overrides this per call.",
            ),
            FieldSpec(
                "manage_git",
                "Managed git",
                "select",
                options=["false", "true"],
                default="false",
                advanced=True,
                help="Framework owns branch/commit/push/PR (ADR 0076); the coder only edits "
                "files. Needs workdir to be a git checkout with an `origin` remote.",
            ),
            FieldSpec(
                "base_branch",
                "Base branch",
                "text",
                placeholder="main",
                advanced=True,
                help="Managed git: branches are cut from fresh origin/<base> and PRs target it.",
            ),
            FieldSpec(
                "branch_prefix",
                "Branch prefix",
                "text",
                placeholder="(delegate name)",
                advanced=True,
                help="Managed git: branch names are <prefix>/<slug>-<id7>. Empty ⇒ the delegate's name.",
            ),
            FieldSpec(
                "return_diff",
                "Return diff",
                "select",
                options=["true", "false"],
                default="true",
                advanced=True,
                help="Unmanaged git: when delegate_to(project=…) sends this coder into a registered "
                "git project, append what it changed (stat + unified diff, capped) to its reply.",
            ),
            *_env_fields(),
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
        _parse_env(raw, d)
        try:
            d.timeout_s = float(raw.get("timeout_s") or 1800)
        except (TypeError, ValueError):
            d.timeout_s = 1800.0
        d.permissions = str(raw.get("permissions", "auto")).strip().lower() or "auto"
        d.allow_kinds = [str(k).lower() for k in (raw.get("allow_kinds") or [])]
        d.deny_kinds = [str(k).lower() for k in (raw.get("deny_kinds") or [])]
        d.confirm = str(raw.get("confirm", "")).strip().lower() in ("1", "true", "yes")
        d.manage_git = str(raw.get("manage_git", "")).strip().lower() in ("1", "true", "yes")
        d.base_branch = str(raw.get("base_branch") or "main").strip() or "main"
        d.branch_prefix = str(raw.get("branch_prefix", "")).strip()
        d.return_diff = str(raw.get("return_diff", "true")).strip().lower() not in ("0", "false", "no", "off")
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
            "env_remove": d.env_remove,
            "permissions": d.permissions,
            "allow_kinds": d.allow_kinds,
            "deny_kinds": d.deny_kinds,
            "conversation_key": d.conversation_key,
            "permissions_ceiling": d.permissions_ceiling,
        }

    async def dispatch(
        self,
        d: Delegate,
        query: str,
        *,
        timeout: float | None = None,
        item_id: str | None = None,
        resume_task_id: str | None = None,
    ) -> str:
        if d.manage_git:
            return await self._dispatch_managed(d, query, timeout=timeout, item_id=item_id)
        if d.capture_diff:
            return await self._prompt_with_changes(d, query, timeout=timeout)
        return await self._prompt(d, query, timeout=timeout)

    async def _prompt_with_changes(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        """Unmanaged dispatch that hands back what the coder changed (``return_diff``).

        Snapshots the workdir's git tree before and after the turn and appends the
        difference to the reply (``change_summary``). Capture is best-effort: a
        non-git workdir or a git failure degrades to a one-line note, never to a
        failed delegation — the coder's work already happened either way."""
        from . import change_summary as cs

        root = os.path.expanduser(d.workdir)
        if not await asyncio.to_thread(cs.is_git_worktree, root):
            reply = await self._prompt(d, query, timeout=timeout)
            return f"{reply}\n\n[no change summary — {root} is not a git repository]"
        try:
            before = await asyncio.to_thread(cs.snapshot, root)
        except (cs.ChangeCaptureError, OSError) as exc:
            reply = await self._prompt(d, query, timeout=timeout)
            return f"{reply}\n\n[no change summary — pre-dispatch snapshot failed: {exc}]"
        token = cs.begin(root)
        try:
            reply = await self._prompt(d, query, timeout=timeout)
        finally:
            overlapped = cs.end(root, token)
        try:
            summary = await asyncio.to_thread(
                lambda: cs.render(before, cs.after(before), project=d.project_name, overlapped=overlapped)
            )
        except (cs.ChangeCaptureError, OSError) as exc:
            summary = f"[no change summary — post-dispatch snapshot failed: {exc}]"
        return f"{reply}\n\n{summary}"

    async def _prompt(self, d: Delegate, query: str, *, timeout: float | None = None) -> str:
        # Reuse the ADR 0024 ACP client + by-kind permission policy.
        from plugins.coding_agent import _client_for, _drop_client, _make_permission
        from plugins.coding_agent.acp_client import AcpError

        spec = self._spec(d)
        client = _client_for(spec)
        client._permission = _make_permission(spec)
        try:
            reply = await client.prompt(query, timeout=timeout or d.timeout_s)
            # getattr: the pool hands back whatever client the spec resolves to, and a
            # missing stop reason must degrade to 'no marker', never to an AttributeError
            # that turns a working delegate into a hard dispatch failure.
            return _mark_incomplete(reply, getattr(client, "last_stop_reason", None))
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
            # Attribute it. `delegate_to` renders a DelegateError as a bare `Error: <msg>`,
            # so without the name a fan-out across several coders can't tell which one blew up.
            raise DelegateError(f"delegate {d.name!r} ({d.command}): {exc}") from exc

    async def dispatch_tapped(
        self,
        d: Delegate,
        prompt: str,
        *,
        on_tool: ToolCallback | None = None,
        on_thought: ProgressCallback | None = None,
        on_text: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> TappedResult:
        """One fully-tapped coder turn — live tool/thought/text callbacks plus the wire
        signals (usage, plan, stop reason, dead end) as a ``TappedResult``.

        The public alternative to reaching into ``plugins.coding_agent``'s private
        client pool (#3235). Everything lifecycle-shaped — fresh-session forgetting,
        the by-kind permission policy, cancel-kills-child, teardown on every exit —
        is owned by ``plugins.coding_agent.dispatch_tapped``; this wrapper only hands
        it the parsed ``Delegate`` (whose ``timeout_s`` becomes the default budget)
        and attributes failures the way ``dispatch`` does. Deliberately SEPARATE from
        ``dispatch``: that path's contract (pooled sessions, managed git, the
        incomplete-reply marker) is unchanged — a tapped turn is one-shot by design,
        and its stop reason rides the result rather than being folded into the text.
        """
        from plugins.coding_agent import dispatch_tapped
        from plugins.coding_agent.acp_client import AcpError

        try:
            return await dispatch_tapped(
                d, prompt, on_tool=on_tool, on_thought=on_thought, on_text=on_text, timeout=timeout
            )
        except AcpError as exc:
            # Same attribution as `_prompt`: name the delegate and its command, so a
            # fan-out across several coders can tell which one blew up.
            raise DelegateError(f"delegate {d.name!r} ({d.command}): {exc}") from exc

    async def _dispatch_managed(
        self, d: Delegate, query: str, *, timeout: float | None = None, item_id: str | None = None
    ) -> str:
        """Managed-git dispatch (ADR 0076): claim → PR pre-flight → branch setup →
        edit-only coder run → framework-owned commit/rebase/push/PR. The claim dedups
        in-flight duplicates (fan-out of one item); the PR pre-flight dedups across
        restarts. On a coder failure the worktree keeps its edits and a re-run's
        idempotent lifecycle adopts them."""
        from plugins.coding_agent import git_harness as harness

        iid = (item_id or "").strip() or harness.derive_item_id(query)
        # Name the work, not the wrapper: infer a conventional-commit title from the task
        # (project_board wraps features in a coder preamble whose first line is boilerplate,
        # which the deterministic first-line slug would otherwise bake into the branch/PR).
        # Best-effort — falls back to the deterministic title. Uniqueness is the id suffix.
        title = (await harness.infer_title(query)) or harness.title_from(query)
        branch = harness.mint_branch(d.branch_prefix or d.name, title, iid)
        holder = harness.claim(iid, d.name)
        if holder is not None:
            return (
                f"Item `{iid}` is already being built by {holder!r} (branch `{branch}`) — not "
                "dispatching a duplicate. Wait for the in-flight run instead of re-fanning this item."
            )
        try:
            workdir = os.path.expanduser(d.workdir)
            existing = await harness.preflight_pr(workdir, iid)
            if existing:
                return f"An open PR already exists for item `{iid}` (branch `{branch}`): {existing} — not dispatching a duplicate."
            prep = await harness.prepare(workdir, base=d.base_branch, branch=branch)
            if prep.error:
                return f"Error: managed-git setup for {d.name!r} failed: {prep.error}"
            reply = await self._prompt(d, query + harness.edit_only_directive(branch), timeout=timeout)
            outcome = await harness.finish(workdir, base=d.base_branch, branch=branch, item_id=iid, title=title)
            notes = "".join(f"\n- note: {n}" for n in prep.notes)
            return f"{reply}\n\n{outcome.render()}{notes}"
        finally:
            harness.release(iid)

    async def teardown(self, d: Delegate) -> bool:
        """Evict + terminate the cached ACP subprocess for this delegate.

        ``dispatch`` caches a long-lived ``AcpClient`` (subprocess + session) keyed
        partly on ``workdir``. A caller that dispatches into a transient, per-call
        ``workdir`` should call this when done (e.g. in a ``finally``) so the child
        is reaped rather than left running — a plain cache ``pop`` forgets the
        handle but leaves the process alive. Returns True if a live client was
        closed; no-op (False) if none was started. Idempotent."""
        from plugins.coding_agent import evict_clients

        return await evict_clients(self._spec(d))

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
            # A binary that isn't an ACP server typically prints its usage/error to stderr
            # and then waits — indistinguishable from a slow handshake unless we show what
            # it said. The client keeps a tail of that stream for exactly this.
            tail = client.stderr_tail()
            hint = f"\nIts stderr:\n{tail}" if tail else ""
            return {"ok": False, "error": f"ACP handshake timed out — does {d.command!r} speak ACP?{hint}"}
        except Exception as exc:  # noqa: BLE001 — spawn/handshake failure → tool-visible string
            return {"ok": False, "error": f"ACP handshake failed: {type(exc).__name__}: {exc}"}
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        return {"ok": True, "detail": f"ACP handshake OK (protocol {client._protocol_version}) — {d.command}"}
