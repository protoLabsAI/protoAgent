"""LangChain/LangGraph tool adapters for protoAgent.

This is the integration point between the A2A handler and your agent's
business logic. Each ``@tool`` function becomes a LangGraph node that
the lead agent can invoke during a run.

The template ships with a small starter set of free, keyless tools so
a fresh clone can demonstrate real agent behaviour out of the box:

- ``current_time`` — wall-clock time in any IANA timezone
- ``calculator`` — safe numeric expression evaluation
- ``web_search`` — DuckDuckGo text search (via ``ddgs``, no API key)
- ``fetch_url`` — fetch a URL and return cleaned text

Plus memory tools that bind to a ``KnowledgeStore`` (constructed in
``server.py`` and threaded through ``get_all_tools(knowledge_store)``):

- ``memory_ingest`` — store a fact / preference / note
- ``memory_recall`` — search the store for relevant chunks
- ``memory_list``   — list recent chunks (optionally per domain)
- ``memory_stats``  — per-domain counts
- ``daily_log``     — convenience: write a daily-log chunk

Replace or extend this file with your agent's real tools and update
``get_all_tools()`` to return the full list.

Every tool that hits an external service should:

- Require explicit identifiers on every call — don't silently fall
  back to env-var defaults for something like ``repo`` / ``project``.
  (An LLM that forgets to pass ``repo`` and picks up a global default
  will fire the call at the wrong target every time.)
- Return clear error strings on failure (the LLM reads them and
  retries) rather than raising — exceptions bubble to the A2A
  handler's ``_deliver_webhook`` path and may surface as 500s.
- Log tool invocations at INFO — ``AuditMiddleware`` already stamps
  duration + success/failure, but domain-specific logs go here.
"""

from __future__ import annotations

import ast
import asyncio
import operator as _op
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

from tools.fallbacks import with_fallback


# ── current_time ─────────────────────────────────────────────────────────────


@tool
@with_fallback()
async def current_time(timezone: str = "UTC") -> str:
    """Return the current wall-clock time in the given IANA timezone.

    Args:
        timezone: An IANA timezone name (e.g. ``"UTC"``, ``"America/New_York"``,
            ``"Europe/London"``, ``"Asia/Tokyo"``). Defaults to UTC.

    Returns ISO-8601 with the timezone offset, plus a human-readable line.
    Use this any time you need to reason about "now" — LLMs cannot
    infer the current time from their training data.
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return f"Error: unknown timezone {timezone!r}. Use an IANA name like 'UTC' or 'America/New_York'."

    now = datetime.now(tz)
    return (
        f"{now.isoformat()} ({timezone})\n"
        f"Human: {now.strftime('%A, %B %d %Y, %H:%M:%S %Z')}"
    )


# ── calculator ───────────────────────────────────────────────────────────────
#
# AST-based safe eval — never calls Python's built-in eval(). Supports
# arithmetic, comparison, power, modulo, and unary negation. No names,
# no attribute access, no calls.

_BIN_OPS: dict[type, object] = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}
_UNARY_OPS: dict[type, object] = {
    ast.UAdd: _op.pos,
    ast.USub: _op.neg,
}


def _safe_eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported binary op: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported unary op: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


@tool
@with_fallback()
async def calculator(expression: str) -> str:
    """Evaluate a numeric expression and return the result.

    Supports ``+ - * / // % **`` and unary ``-``. No names, no function
    calls, no variables — this is a pocket calculator, not a REPL.

    Args:
        expression: A Python-style arithmetic expression, e.g.
            ``"1 + 2 * 3"``, ``"(100 - 12.5) / 7"``, ``"2 ** 10"``.

    Returns a string with the result, or a readable error.
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree)
    except SyntaxError:
        return f"Error: not a valid expression: {expression!r}"
    except ZeroDivisionError:
        return "Error: division by zero"
    except Exception as e:
        return f"Error: {e}"
    return f"{expression} = {result}"


# ── web_search (DuckDuckGo) ──────────────────────────────────────────────────


@tool
@with_fallback()
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return a list of result summaries.

    Free, no API key required. Rate-limited by DuckDuckGo — don't hammer.

    Args:
        query: Search query string.
        max_results: How many results to return (1–10, default 5).

    Returns a numbered list of ``title — url\\nsnippet`` entries, or
    a readable error if the search fails (network, rate-limit, etc.).
    """
    max_results = max(1, min(max_results, 10))
    try:
        from ddgs import DDGS
    except ImportError:
        return (
            "Error: the 'ddgs' package is not installed. Add `ddgs>=9.0` to "
            "requirements.txt and rebuild the image."
        )

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"Error: DuckDuckGo search failed: {e}"

    if not results:
        return f"No results for {query!r}."

    lines = [f"{len(results)} result(s) for {query!r}:"]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "(no title)"
        url = (r.get("href") or r.get("url") or "").strip()
        body = (r.get("body") or "").strip()
        lines.append(f"{i}. {title} — {url}")
        if body:
            lines.append(f"   {body}")
    return "\n".join(lines)


# ── fetch_url ────────────────────────────────────────────────────────────────


_MAX_FETCH_BYTES = 2_000_000  # 2MB — enough for most articles, caps blast radius
_MAX_OUTPUT_CHARS = 8000      # LLM context budget; callers can ask for a shorter limit


@tool
@with_fallback()
async def fetch_url(url: str, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    """Fetch a URL and return its main text content.

    Strips scripts, styles, and HTML markup. Truncates at ``max_chars``
    so a single fetch can't blow the LLM context budget.

    Args:
        url: Absolute http(s) URL to fetch.
        max_chars: Max characters of text to return (default 8000).

    Returns the extracted text, or a readable error. Pairs with
    ``web_search`` — search to find URLs, fetch to read them.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Error: url must start with http:// or https:// — got {url!r}"

    try:
        import httpx
    except ImportError:
        return "Error: httpx not installed — cannot fetch URLs."

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15, headers={
                "User-Agent": "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)",
            },
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return f"Error: fetch failed: {e}"

    if resp.status_code >= 400:
        return f"Error: HTTP {resp.status_code} for {url}"

    content = resp.content[:_MAX_FETCH_BYTES]
    ctype = (resp.headers.get("content-type") or "").lower()

    if "html" in ctype or content.lstrip().startswith(b"<"):
        text = _extract_text_from_html(content)
    else:
        try:
            text = content.decode(resp.encoding or "utf-8", errors="replace")
        except LookupError:
            text = content.decode("utf-8", errors="replace")

    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…[truncated]"

    return f"[{resp.status_code}] {url}\n\n{text}"


def _extract_text_from_html(content: bytes) -> str:
    """Strip HTML to plain text. Uses BeautifulSoup when available, falls
    back to a simple tag-stripping regex otherwise."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        import re
        raw = content.decode("utf-8", errors="replace")
        # Remove script/style blocks first so their contents don't leak through
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<[^>]+>", " ", raw)
        return re.sub(r"\s+", " ", raw)

    soup = BeautifulSoup(content, "html.parser")
    for el in soup(["script", "style", "nav", "footer", "noscript"]):
        el.decompose()
    # Prefer <main> / <article> when the page uses them; otherwise whole body
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [line.strip() for line in main.get_text("\n").splitlines() if line.strip()]
    return "\n".join(lines)


# ── memory tools ─────────────────────────────────────────────────────────────
#
# Each memory tool is built by a factory that closes over the
# ``KnowledgeStore`` instance. Doing it this way (rather than module-
# level globals) keeps tests isolated — they pass a temp store and get
# a fresh tool list bound to it. Production constructs one store in
# ``server.py`` and reuses the bound tools for the lifetime of the
# process.


_MEMORY_RECALL_MAX_K = 20
_MEMORY_LIST_MAX_LIMIT = 200

# Stable list of scheduler tool names. Exposed as a module-level
# constant so ``graph/config_io.py::list_available_tools`` can show
# the wizard the right surface even when the runtime hasn't yet
# constructed a scheduler instance (e.g. fresh boot before setup is
# complete). Keep in sync with ``_build_scheduler_tools``.
SCHEDULER_TOOL_NAMES: tuple[str, ...] = (
    "schedule_task", "list_schedules", "cancel_schedule",
)
MEMORY_TOOL_NAMES: tuple[str, ...] = (
    "memory_ingest", "memory_recall", "memory_list", "memory_stats", "daily_log",
)
INBOX_TOOL_NAMES: tuple[str, ...] = ("check_inbox",)


def _build_inbox_tools(inbox_store) -> list:
    """Bind the inbox tool to an ``InboxStore`` (ADR 0003). Returns a list."""

    @tool
    async def check_inbox(priority_floor: str = "next", limit: int = 10) -> str:
        """Pull pending inbound messages (webhooks, external systems, sister
        agents) from the inbox and mark them delivered.

        Inbound items arrive with a priority tier: ``now`` items already fired a
        turn; ``next`` items wait for you to surface them; ``later`` items are
        background. Call this when the operator asks "anything new?" or when the
        conversation suggests checking for outside input.

        Args:
            priority_floor: ``"now"`` (now only), ``"next"`` (now + next, the
                default), or ``"later"`` (everything pending).
            limit: Max items to return (default 10).

        Returns the items one per line, or ``"Inbox empty."`` when there's
        nothing pending at that floor.
        """
        floor = priority_floor if priority_floor in ("now", "next", "later") else "next"
        items = inbox_store.list(priority_floor=floor, limit=max(1, min(int(limit), 50)))
        if not items:
            return "Inbox empty."
        inbox_store.mark_delivered([i["id"] for i in items])
        lines = []
        for i in items:
            src = f" (from {i['source']})" if i.get("source") else ""
            lines.append(f"[{i['priority']}]{src} {i['text']}")
        return "\n".join(lines)

    return [check_inbox]


def _build_memory_tools(knowledge_store) -> list:
    """Bind memory tools to a ``KnowledgeStore``. Returns a list."""
    from datetime import datetime, timezone

    @tool
    async def memory_ingest(
        content: str,
        domain: str = "general",
        heading: str | None = None,
    ) -> str:
        """Store a fact, preference, or note in long-term memory.

        Use this for things the operator wants you to remember across
        sessions — preferences ("I take my coffee black"), facts about
        the operator's environment, decisions worth recalling later.

        Args:
            content: The text to remember. Be specific and self-contained;
                the chunk is retrieved by keyword search.
            domain: Logical bucket — ``"preferences"``, ``"context"``,
                ``"general"``. Defaults to ``"general"``.
            heading: Optional short label (e.g. ``"coffee"``) used as a
                stable de-dupe key by the eval suite and curator.

        Returns ``"Stored chunk N in 'domain'."`` on success.
        """
        chunk_id = knowledge_store.add_chunk(content, domain=domain, heading=heading)
        if chunk_id is None:
            return "Error: failed to store chunk (knowledge store unavailable)."
        return f"Stored chunk {chunk_id} in {domain!r}."

    @tool
    async def memory_recall(query: str, k: int = 5) -> str:
        """Search long-term memory for chunks relevant to ``query``.

        Returns the top-k matches, one per line. Pull this when the
        operator asks something where stored context is more reliable
        than the model's own training data ("what's my coffee order?",
        "remind me what we decided about the auth migration").

        Returns ``"No matches."`` when the store is empty or nothing
        scores above the keyword threshold.
        """
        clamped_k = max(1, min(int(k), _MEMORY_RECALL_MAX_K))
        results = knowledge_store.search(query, k=clamped_k)
        if not results:
            return "No matches."
        lines = [f"[{r.get('domain', '?')}] {r['preview']}" for r in results]
        return "\n".join(lines)

    @tool
    async def memory_list(domain: str | None = None, limit: int = 10) -> str:
        """List the most recent chunks. Filter by domain when given.

        Useful when the operator asks for recent activity ("what did I
        log today?") or wants to inspect what the agent has stored.
        """
        clamped_limit = max(1, min(int(limit), _MEMORY_LIST_MAX_LIMIT))
        chunks = knowledge_store.list_chunks(domain=domain, limit=clamped_limit)
        if not chunks:
            return f"No chunks in {domain or 'any domain'}."
        lines = []
        for c in chunks:
            head = f"[{c.domain}]"
            if c.heading:
                head += f" {c.heading}:"
            preview = (c.content or "")[:200]
            lines.append(f"{c.created_at} {head} {preview}")
        return "\n".join(lines)

    @tool
    async def memory_stats() -> str:
        """Return chunk counts per domain. Useful for sanity checks."""
        s = knowledge_store.stats()
        if s.get("total", 0) == 0:
            return "Knowledge store is empty."
        lines = [f"Total: {s['total']}"]
        for k, v in s.items():
            if k == "total":
                continue
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @tool
    async def daily_log(content: str) -> str:
        """Append a daily-log entry for today.

        Stored under ``domain='daily-log'`` with today's UTC date as
        the heading, so the same day's entries cluster together for
        ``memory_list(domain='daily-log')`` queries.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        chunk_id = knowledge_store.add_chunk(
            content, domain="daily-log", heading=today,
        )
        if chunk_id is None:
            return "Error: failed to write daily log entry."
        return f"Logged ({today}): {content[:120]}"

    return [memory_ingest, memory_recall, memory_list, memory_stats, daily_log]


# ── scheduler tools ──────────────────────────────────────────────────────────
#
# Three tools that bind to either the local sqlite-backed scheduler or
# the Workstacean adapter — the agent loop sees one stable surface and
# never has to know which backend is wired up.
#
# Multi-agent safety: the underlying backend is constructed in
# ``server.py`` with the active ``AGENT_NAME`` baked in. add_job /
# list_jobs / cancel_job all filter by that name so two protoAgent
# instances on the same machine (or sharing one Workstacean install)
# never see each other's jobs.


def _build_scheduler_tools(scheduler) -> list:
    """Bind scheduler tools to a ``SchedulerBackend``. Returns a list."""

    @tool
    async def schedule_task(
        prompt: str,
        when: str,
        job_id: str | None = None,
    ) -> str:
        """Schedule a future task. The agent receives ``prompt`` as a
        new turn when the schedule fires.

        Use this for anything the operator wants done later: reminders
        ("remind me to follow up on the auth migration tomorrow at
        9am"), recurring sweeps ("every Monday morning, summarize last
        week's logs"), one-off check-ins ("at 3pm today, ask whether
        the deploy is healthy").

        Args:
            prompt: The text the agent should receive when the schedule
                fires. Be self-contained — the agent has no memory of
                this scheduling moment when the task fires.
            when: Either a 5-field cron expression (``"0 9 * * 1-5"``
                = every weekday at 9am) or an ISO-8601 datetime
                (``"2026-05-01T15:00:00"`` = once at 3pm UTC on May 1).
                Compute exact times using ``current_time`` — the agent
                cannot infer "now" from training data.
            job_id: Optional human-readable id for the job. Auto-
                generated if omitted; you'll need it later to cancel.

        Returns ``"Scheduled job <id> next at <iso>."`` on success,
        an error string on malformed ``when`` or backend failure.
        """
        try:
            job = await asyncio.to_thread(scheduler.add_job, prompt, when, job_id=job_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: scheduler add_job failed: {exc}"
        next_fire = job.next_fire or "(managed by remote scheduler)"
        return f"Scheduled job {job.id} next at {next_fire}."

    @tool
    async def list_schedules() -> str:
        """List the current scheduled jobs for this agent.

        Returns one job per line with id, next-fire timestamp, and a
        prompt preview. Returns ``"No scheduled jobs."`` when empty.

        Backends that delegate state to a remote scheduler (e.g. the
        Workstacean adapter) may return an empty list even when jobs
        exist — query the remote scheduler directly to see those.
        """
        jobs = await asyncio.to_thread(scheduler.list_jobs)
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            preview = (j.prompt or "")[:80]
            next_fire = j.next_fire or "(managed remotely)"
            lines.append(f"{j.id}  next={next_fire}  schedule={j.schedule!r}  {preview}")
        return "\n".join(lines)

    @tool
    async def cancel_schedule(job_id: str) -> str:
        """Cancel a scheduled job by id.

        Args:
            job_id: The id returned by ``schedule_task`` (or shown by
                ``list_schedules``).

        Returns ``"Canceled <id>."`` or ``"Error: no such job <id>."``.
        """
        if not job_id or not job_id.strip():
            return "Error: job_id is required."
        try:
            ok = await asyncio.to_thread(scheduler.cancel_job, job_id)
        except Exception as exc:  # noqa: BLE001
            return f"Error: scheduler cancel_job failed: {exc}"
        return f"Canceled {job_id}." if ok else f"Error: cancel failed or no such job {job_id}."

    return [schedule_task, list_schedules, cancel_schedule]


# ── registry ─────────────────────────────────────────────────────────────────


def get_all_tools(knowledge_store=None, scheduler=None, inbox_store=None):
    """Return every LangChain tool the lead agent + subagents can use.

    Optional dependencies:

    - ``knowledge_store`` enables the memory tools (memory_ingest,
      memory_recall, memory_list, memory_stats, daily_log).
    - ``scheduler`` enables the scheduler tools (schedule_task,
      list_schedules, cancel_schedule). Accepts any backend that
      implements ``scheduler.interface.SchedulerBackend``.

    Pass ``None`` to disable either subsystem — the lead agent runs
    fine with just the four keyless general tools.
    """
    tools = [current_time, calculator, web_search, fetch_url]
    # GitHub read tools (PRs/issues/commits) over the gh CLI. Always
    # included — they degrade to a readable error if gh/auth is missing.
    from tools.github_tools import get_github_tools
    tools.extend(get_github_tools())
    # Project-notes tools — read/write the operator console's Notes panel tabs,
    # gated per-tab by the operator's Agent-read/write toggles. Always included
    # so "what's on my Todo tab?" reaches the notes, not the memory store.
    from tools.notes_tools import get_notes_tools
    tools.extend(get_notes_tools())
    # A2A peer-consult tools — only when at least one PEER_<HANDLE>_URL is set,
    # so agents with no federation peers aren't cluttered.
    from tools.peer_tools import get_peer_tools, list_env_peers
    if list_env_peers():
        tools.extend(get_peer_tools())
    if knowledge_store is not None:
        tools.extend(_build_memory_tools(knowledge_store))
    if scheduler is not None:
        tools.extend(_build_scheduler_tools(scheduler))
    if inbox_store is not None:
        tools.extend(_build_inbox_tools(inbox_store))
    return tools
