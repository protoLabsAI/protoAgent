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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tools.fallbacks import with_fallback


# ── current_time ─────────────────────────────────────────────────────────────


@tool
def ask_human(question: str) -> str:
    """Pause and ask the human operator a question, then continue with their answer.

    Use this only when you genuinely need a human decision or a fact you cannot
    determine yourself — an approval ("merge this PR?"), a missing input, or a
    choice between options. The task pauses (surfaced to A2A callers as the
    ``input-required`` state) until the operator answers; their reply is returned
    from this call so you can continue. Do NOT use it for narration or status —
    only for an answer you must wait on. Phrase ``question`` as a clear,
    self-contained ask.
    """
    # LangGraph HITL: interrupt() checkpoints the graph at this exact point. On
    # resume (Command(resume=answer)) it returns the operator's reply. Requires a
    # checkpointer (bound at compile) — which protoAgent always has.
    from langgraph.types import interrupt

    answer = interrupt({"question": question})
    return answer if isinstance(answer, str) else str(answer)


@tool
def request_user_input(title: str, steps: list[dict], description: str = "") -> str:
    """Ask the operator for **structured** input via a form dialog, then continue
    with their response. Use when you need specific values, choices, credentials,
    or config — anything better captured as form fields than free text. The task
    pauses (surfaced as ``input-required``) until they submit; their response (a
    JSON object keyed by field name) is returned from this call.

    ``steps`` is a list of form steps — multiple steps render as a wizard. Each
    step is ``{"schema": <JSON Schema draft-07 of the fields>, "uiSchema"?: <layout
    hints>, "title"?: str, "description"?: str}``. Phrase the ask clearly and only
    request fields you actually need. For a single free-text or yes/no question,
    use ``ask_human`` instead.
    """
    import json
    from langgraph.types import interrupt

    response = interrupt(
        {
            "kind": "form",
            "title": title,
            "description": description,
            "steps": steps,
        }
    )
    # The resume value is the submitted form object; return it as JSON so the
    # model reads structured fields. (A plain string resume is passed through.)
    return response if isinstance(response, str) else json.dumps(response)


# show_component (inline component rendering, ADR 0051) is TEMPORARILY DISABLED — see
# https://github.com/protoLabsAI/protoAgent/issues/1323. The component-v1 pipeline
# (graph/components.py codec, the server/chat.py sentinel extraction, the console
# component registry) is left intact; re-enable by uncommenting this tool AND its entry
# in the get_all_tools() list below.
#
# @tool
# def show_component(component: str, props: dict, title: str = "") -> str:
#     """Render a structured UI component inline in the chat (ADR 0051).
#
#     Use this to present structured data as a real widget instead of a markdown blob —
#     a comparison table, a status/metrics block, a plan/timeline. Data-only and safe;
#     for free-form generated HTML use an artifact instead.
#
#     Args:
#         component: one of ``"table"``, ``"keyvalue"``, ``"timeline"``.
#         props: the component's data:
#             - table:    ``{"columns": ["A","B"], "rows": [["a1","b1"], ...]}``
#             - keyvalue: ``{"items": [{"label": "Credits", "value": "183k"}, ...]}``
#             - timeline: ``{"steps": [{"label": "Buy hauler", "state": "done|active|todo",
#                           "detail": "…"}, ...]}``
#         title: optional heading shown above the component.
#
#     Renders immediately for the user; also briefly summarize the data in your text reply
#     (the component is a visual aid, not a substitute for your answer).
#     """
#     from graph.components import COMPONENT_TYPES, encode_component
#
#     if component not in COMPONENT_TYPES:
#         return f"Error: unknown component '{component}'. Use one of: {', '.join(COMPONENT_TYPES)}."
#     payload = dict(props or {})
#     if title and "title" not in payload:
#         payload["title"] = title
#     return f"Rendered a {component} component for the user. " + encode_component(component, payload)


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
    return f"{now.isoformat()} ({timezone})\nHuman: {now.strftime('%A, %B %d %Y, %H:%M:%S %Z')}"


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
        return "Error: the 'ddgs' package is not installed. Add `ddgs>=9.0` to requirements.txt and rebuild the image."

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
_MAX_OUTPUT_CHARS = 8000  # LLM context budget; callers can ask for a shorter limit


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

    # Egress allowlist (ADR 0008) — deny-by-default when configured; permissive
    # (no-op) otherwise. fetch_url is the model-chosen-host exfil/SSRF vector.
    from security import egress

    blocked = egress.check_url(url)
    if blocked:
        return blocked

    try:
        import httpx
    except ImportError:
        return "Error: httpx not installed — cannot fetch URLs."

    try:
        # Disable auto-redirects and follow manually so each hop's host is
        # re-checked against egress — otherwise a public URL that 30x-redirects
        # to http://169.254.169.254/ would bypass the SSRF guard above.
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=15,
            headers={
                "User-Agent": "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)",
            },
        ) as client:
            resp = await client.get(url)
            hops = 0
            while resp.is_redirect and hops < 5:
                nxt = str(resp.url.join(resp.headers.get("location") or ""))
                if not (nxt.startswith("http://") or nxt.startswith("https://")):
                    return f"Error: refusing non-http(s) redirect to {nxt!r}"
                blocked = egress.check_url(nxt)
                if blocked:
                    return blocked
                resp = await client.get(nxt)
                hops += 1
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

# Fork tool denylist — names dropped from ``get_all_tools``. Set once from config
# (``tools.disabled``) by ``set_disabled_tools`` at config load/reload, so a fork
# removes core tools via YAML instead of editing ``get_all_tools`` (a core edit
# that would conflict on every upstream re-sync). Plugins still ADD tools.
_disabled_tools: set[str] = set()


def set_disabled_tools(names) -> None:
    """Set the fork tool denylist (config ``tools.disabled``)."""
    global _disabled_tools
    _disabled_tools = {str(n).strip() for n in (names or []) if str(n).strip()}


# Stable list of scheduler tool names. Exposed as a module-level
# constant so ``graph/config_io.py::list_available_tools`` can show
# the wizard the right surface even when the runtime hasn't yet
# constructed a scheduler instance (e.g. fresh boot before setup is
# complete). Keep in sync with ``_build_scheduler_tools``.
SCHEDULER_TOOL_NAMES: tuple[str, ...] = (
    "schedule_task",
    "list_schedules",
    "cancel_schedule",
)
MEMORY_TOOL_NAMES: tuple[str, ...] = (
    "memory_ingest",
    "memory_recall",
    "memory_list",
    "memory_stats",
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
        # add_chunk embeds over HTTP on hybrid stores — keep it off the loop.
        import asyncio

        chunk_id = await asyncio.to_thread(knowledge_store.add_chunk, content, domain=domain, heading=heading)
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
        # search embeds the query over HTTP on hybrid stores — keep it off the loop.
        import asyncio

        results = await asyncio.to_thread(knowledge_store.search, query, k=clamped_k)
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
            # Lead with the chunk id so a caller (e.g. the `dream` consolidation
            # pass) can target a stale/superseded fact with `forget_memory`.
            lines.append(f"#{c.id} {c.created_at} {head} {preview}")
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
    async def forget_memory(chunk_id: int, reason: str = "") -> str:
        """Delete ONE long-term-memory chunk by id (the `#<id>` shown by
        memory_list). The consolidation/forgetting half of a `/dream` pass: use
        it to remove a fact that is stale, superseded, or a duplicate — ideally
        after `memory_ingest`-ing the corrected/merged version first.

        Targeted and deliberate by design: it deletes exactly the one id you
        pass (no bulk/wildcard delete), so review with memory_list and forget
        only what you're sure is no longer worth keeping. `reason` is for your
        own audit trail. Returns whether a chunk was removed.
        """
        try:
            cid = int(chunk_id)
        except (TypeError, ValueError):
            return f"Error: chunk_id must be an integer (got {chunk_id!r})."
        removed = knowledge_store.delete_by_id(cid)
        if not removed:
            return f"No memory chunk #{cid} found — nothing deleted."
        return f"Forgot memory chunk #{cid}." + (f" ({reason})" if reason else "")

    return [memory_ingest, memory_recall, memory_list, memory_stats, forget_memory]


# ── scheduler tools ──────────────────────────────────────────────────────────
#
# Three tools that bind to the local sqlite-backed scheduler — the agent loop
# sees one stable surface over the SchedulerBackend protocol.
#
# Multi-agent safety: the underlying backend is constructed in
# ``server.py`` with the active ``AGENT_NAME`` baked in. add_job /
# list_jobs / cancel_job all filter by that name so two protoAgent
# instances on the same machine never see each other's jobs.


def _build_scheduler_tools(scheduler) -> list:
    """Bind scheduler tools to a ``SchedulerBackend``. Returns a list."""

    @tool
    async def schedule_task(
        prompt: str,
        when: str,
        job_id: str | None = None,
        timezone: str | None = None,
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
            timezone: Optional IANA timezone (e.g. ``"America/Chicago"``)
                the cron expression is evaluated in, handling DST — so
                ``"0 9 * * *"`` means 9am local. Omit for UTC. Ignored
                for one-shot ISO times (those carry their own offset).

        Returns ``"Scheduled job <id> next at <iso>."`` on success,
        an error string on malformed ``when`` / ``timezone`` or backend failure.
        """
        # Dedup guard: don't create a second job identical to an existing active
        # one (same prompt + schedule + timezone). This is the common cause of
        # scheduled-task spam — a loop that re-schedules itself on each run/restart
        # accumulates duplicates that all fire together. (Remote backends may return
        # [] here; then we skip the check and let the backend own dedup.)
        try:
            for j in await asyncio.to_thread(scheduler.list_jobs):
                if (
                    getattr(j, "enabled", True)
                    and (j.prompt or "").strip() == prompt.strip()
                    and j.schedule == when
                    and (getattr(j, "timezone", None) or None) == (timezone or None)
                ):
                    return (
                        f"Already scheduled as {j.id} (next at "
                        f"{j.next_fire or 'managed remotely'}). Not creating a duplicate."
                    )
        except Exception:  # noqa: BLE001 — dedup is best-effort; never block scheduling
            pass
        try:
            job = await asyncio.to_thread(scheduler.add_job, prompt, when, job_id=job_id, timezone=timezone)
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

    @tool
    async def wait(seconds: int, then: str, state: Annotated[Any, InjectedState] = None) -> str:
        """Pause and resume LATER instead of polling. Use this whenever you are
        waiting for something to finish — a ship to arrive, a build/deploy, a
        cooldown, a countdown a status tool reported ("arriving in 37s"). Do NOT
        call a status tool over and over to wait it out; that burns the entire
        turn in one go.

        Calling ``wait`` ENDS your turn immediately and schedules a one-shot
        wake-up ``seconds`` from now. When it fires you are re-invoked with
        ``then`` as your instruction — back in THIS same conversation, with its
        history intact (ADR 0053) — so you act exactly once, when the thing is
        actually ready. This is the right way to run long-horizon work without
        spinning.

        Args:
            seconds: how long to wait, in seconds (e.g. 40). Use the ETA a status
                tool gave you and round up a little. Minimum 1.
            then: the self-contained instruction to run on resume — e.g. "Dock
                NOVAHAUL-5 at X1-UC87-K93, sell the ore, then accept the next
                contract." This is your only context when you wake, so be
                specific about what to do and which entities are involved.

        Returns a confirmation with the resume time. For an absolute time or a
        recurring schedule use ``schedule_task`` instead — ``wait`` is for
        "yield for a bit, then pick this back up".
        """
        if not (then or "").strip():
            return "Error: `then` is required — describe what to do on resume."
        secs = max(1, int(seconds))
        when = (datetime.now(UTC) + timedelta(seconds=secs)).isoformat()
        # Resume in the SAME conversation: stamp the originating chat session
        # (== the turn's A2A contextId) onto the job so the scheduler fires the
        # resume into this thread, not the Activity thread — the agent wakes up
        # with the conversation history intact (ADR 0053). Same contextvar the
        # background-subagent path reads. Empty (e.g. an Activity-origin turn) →
        # the scheduler falls back to the Activity thread.
        # Read the originating session from the injected graph state, NOT the
        # tracing contextvar — the contextvar reads empty in a tool body under
        # LangGraph, which silently dropped this resume to the Activity thread.
        ctx = _session_id_from(state) or None
        try:
            job = await asyncio.to_thread(scheduler.add_job, then, when, context_id=ctx)
        except Exception as exc:  # noqa: BLE001
            return f"Error: couldn't schedule the wake-up: {exc}"
        return f"Yielding for {secs}s — turn ending now. You'll be re-invoked at {job.next_fire or when} to: {then}"

    return [schedule_task, list_schedules, cancel_schedule, wait]


# ── registry ─────────────────────────────────────────────────────────────────


def _build_task_tools(tasks_store) -> list:
    """Bind the tasks issue tracker to a ``TaskStore`` (Sprint B) — the agent's
    in-process planning/task surface. Returns a list."""

    @tool
    def task_create(title: str, description: str = "", priority: int = 2, issue_type: str = "task") -> str:
        """Track a task/issue on your tasks board — your planning surface for
        multi-step work. ``priority`` 0=highest…3=low; ``issue_type`` is one of
        task|bug|feature|chore|epic. Returns the new issue id."""
        try:
            i = tasks_store.create(title, description=description, priority=priority, issue_type=issue_type)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Created {i['id']}: {i['title']} ({i['issue_type']}, p{i['priority']})"

    @tool
    def task_list(include_closed: bool = False) -> str:
        """List issues on your tasks board (open ones by default). Use it to see
        and track outstanding work."""
        items = tasks_store.list(include_closed=include_closed)
        if not items:
            return "No issues on the board."
        return "\n".join(f"[{i['status']}] {i['id']} (p{i['priority']}, {i['issue_type']}) {i['title']}" for i in items)

    @tool
    def task_update(
        issue_id: str,
        status: str = "",
        title: str = "",
        description: str = "",
        priority: int = -1,
        issue_type: str = "",
    ) -> str:
        """Update an issue. ``status`` is open|in_progress|blocked|deferred|closed.
        Leave a field empty (``priority`` -1) to keep it unchanged."""
        fields: dict = {}
        if status:
            fields["status"] = status
        if title:
            fields["title"] = title
        if description:
            fields["description"] = description
        if priority is not None and priority >= 0:
            fields["priority"] = priority
        if issue_type:
            fields["issue_type"] = issue_type
        try:
            i = tasks_store.update(issue_id, **fields)
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"
        return f"Updated {i['id']}: [{i['status']}] {i['title']}"

    @tool
    def task_close(issue_id: str, reason: str = "") -> str:
        """Close an issue (done, or won't-do). Optional ``reason``."""
        try:
            i = tasks_store.close(issue_id, reason=reason or None)
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"
        return f"Closed {i['id']}: {i['title']}"

    return [task_create, task_list, task_update, task_close]


def _session_id_from(state: Any) -> str:
    """Resolve the originating session id from inside a TOOL BODY.

    The graph state (``graph/state.py``) reliably carries ``session_id`` at
    tool-execution time — every turn's graph input stamps it. The
    ``tracing.current_session_id()`` contextvar is visible to MIDDLEWARE but NOT
    to a tool body under LangGraph (the tool runs in a different execution
    context), so it silently reads empty there — which is why ``wait`` dropped
    its same-session resume to the Activity thread (ADR 0053) and why ``set_goal``
    would refuse with "No active session". Prefer the injected state; keep the
    contextvar only as a fallback for tools invoked outside a graph turn."""
    from observability import tracing

    sid = ""
    if isinstance(state, dict):
        sid = (state.get("session_id") or "").strip()
    return sid or (tracing.current_session_id() or "")


def _build_set_goal_tool():
    """The lead agent sets its OWN standing goal — verified by a plugin verifier
    only (ADR 0028). The agent literally can't open a shell/eval goal here: the
    tool hardcodes ``type="plugin"`` and routes through ``set_goal_safe``."""

    @tool
    def set_goal(
        condition: str,
        check: str,
        check_args: dict | None = None,
        max_iterations: int | None = None,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Set a standing goal for THIS session, ground-truthed by a plugin verifier.

        You'll be re-invoked toward `condition` until the plugin verifier named by
        `check` (a registered "<plugin-id>:<name>" verifier) passes. `check_args` is
        declarative data the verifier reads (e.g. {"min": 1000000}). Only plugin
        verifiers are allowed — shell/test/data goals are operator-only via /goal.
        Returns the goal status, or an error if goal mode is off / `check` is unknown.
        """
        from runtime.state import STATE

        if STATE.goal_controller is None:
            return "Goal mode is not enabled."
        session_id = _session_id_from(state)  # injected graph state, not the contextvar
        if not session_id:
            return "No active session — set_goal can only run during a turn."
        # Reject an unknown verifier up front. Otherwise the goal is created but can
        # never pass — it just spins to the iteration cap, flagged 'unachievable'
        # (the live failure mode). List the registered ones so the agent can choose.
        from graph.goals.verifiers import plugin_verifier_names

        known = plugin_verifier_names()
        if check not in known:
            avail = ", ".join(known) if known else "(none registered — enable a plugin that contributes a verifier)"
            return f"Error: unknown plugin verifier {check!r}. Available verifiers: {avail}."
        verifier = {"type": "plugin", "check": check, "args": check_args or {}}
        _ok, msg = STATE.goal_controller.set_goal_safe(
            session_id,
            condition,
            verifier,
            max_iterations,
        )
        return msg

    return set_goal


@tool
def load_skill(name: str) -> str:
    """Load the full step-by-step procedure for a skill.

    The ``<available_skills>`` block in your context lists each skill as a name +
    one-line summary (progressive disclosure, ADR 0060). This returns that skill's
    complete body so you can follow it. Call it the moment you judge a listed skill
    fits the task — *before* acting — and follow the steps it returns; do not guess
    a skill's contents from its summary. ``name`` must match a ``<skill name="…">``
    exactly. Returns an error string (it never raises) when the skill or the index
    is unavailable.
    """
    from runtime.state import STATE

    idx = STATE.skills_index
    if idx is None:
        return "Skills index is not available."
    rec = idx.get_skill((name or "").strip())
    if rec is None:
        # Recover from a typo'd name by offering the discoverable set — capped so a
        # large library can't blow up the error string.
        try:
            names = [s["name"] for s in idx.skill_summaries()]
        except Exception:  # noqa: BLE001
            names = []
        shown = names[:40]
        more = f" (+{len(names) - len(shown)} more — call list_skills)" if len(names) > len(shown) else ""
        hint = f" Available skills: {', '.join(shown)}.{more}" if shown else ""
        return f"No skill named {name!r}.{hint}"

    desc = " ".join((rec.get("description") or "").split())
    body = (rec.get("prompt_template") or "").strip()
    tools_used = rec.get("tools_used") or []
    if isinstance(tools_used, str):
        tools_used = tools_used.split()

    lines = [f"# Skill: {rec.get('name')}"]
    if desc:
        lines.append(desc)
    if tools_used:
        lines.append(f"\nRelevant tools: {', '.join(tools_used)}")
    lines.append(f"\n## Procedure\n{body}" if body else "\n(This skill has no recorded procedure.)")
    return "\n".join(lines)


def _build_curation_tools():
    """Read-mostly tools for the memory/skill curation subagents (`dream` /
    `distill`, ADR 0054). They read from STATE at call time (the ``set_goal``
    pattern) so ``get_all_tools`` needs no new wiring, and they are deliberately
    scoped: two read-only surfaces over what the agent has actually been doing
    (``recent_activity``, ``list_skills``) plus one *additive-only* writer
    (``save_skill``). There is no raw-SQL / shell escape hatch — the whole class
    of "the consolidation agent rewrote the trajectory DB" risk simply can't
    happen here, unlike a bash-driven distill."""

    @tool
    def recent_activity(limit: int = 30, window_hours: int = 168) -> str:
        """Read-only digest of what the agent has recently DONE — for spotting
        repeated workflows or durable facts worth consolidating.

        Combines the Activity feed (recent assistant turns: time · origin ·
        trigger · text) with a telemetry rollup (turn/tool/cost volume, per
        model) over the last ``window_hours`` (default 7 days). Use this as the
        primary evidence source for `/dream` and `/distill`. Purely read-only —
        it never modifies anything.
        """
        from datetime import datetime, timedelta, timezone

        from runtime.state import STATE

        lim = max(1, min(int(limit or 30), 200))
        out: list[str] = []

        ts = STATE.telemetry_store
        if ts is not None:
            try:
                since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(window_hours or 168)))).isoformat()
                s = ts.summary(since_iso=since)
                out.append(
                    f"## Telemetry (last {window_hours}h): {s.get('turns', 0)} turns, "
                    f"{s.get('tool_calls', 0)} tool calls, {s.get('llm_calls', 0)} LLM calls, "
                    f"${s.get('cost_usd', 0.0):.4f}, success {s.get('success_rate', 0.0):.0%}"
                )
                by_model = s.get("by_model") or []
                if by_model:
                    out.append(
                        "By model: "
                        + "; ".join(
                            f"{m.get('model') or '?'} ×{m.get('turns', 0)} (${m.get('cost_usd', 0.0):.4f})"
                            for m in by_model[:8]
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                out.append(f"(telemetry rollup unavailable: {exc})")

        al = STATE.activity_log
        if al is not None:
            rows = al.recent(limit=lim)
            if rows:
                out.append(f"\n## Recent activity ({len(rows)} most recent turns):")
                for r in rows:
                    text = " ".join((r.get("text") or "").split())
                    if len(text) > 240:
                        text = text[:239] + "…"
                    tag = "/".join(p for p in (r.get("origin"), r.get("trigger")) if p)
                    out.append(f"- [{r.get('created_at', '')[:19]}] ({tag or 'operator'}) {text}")
        if not out:
            return (
                "No activity or telemetry is available yet — nothing to consolidate or distill. Report this and stop."
            )
        return "\n".join(out)

    @tool
    def list_skills() -> str:
        """List every skill already in the index (name · source · confidence ·
        description) so a distill/dream pass reuses or extends instead of
        duplicating. Read-only.
        """
        from runtime.state import STATE

        idx = STATE.skills_index
        if idx is None:
            return "Skills index is not available."
        skills = idx.all_skills()
        if not skills:
            return "No skills are indexed yet."
        skills.sort(key=lambda s: (s.get("source") or "", -(s.get("confidence") or 0.0)))
        lines = [f"{len(skills)} skill(s) indexed:"]
        for s in skills:
            desc = " ".join((s.get("description") or "").split())
            if len(desc) > 120:
                desc = desc[:119] + "…"
            conf = s.get("confidence")
            conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
            lines.append(f"- {s.get('name')} [{s.get('source') or '?'} · conf {conf_s}] — {desc}")
        return "\n".join(lines)

    @tool
    def save_skill(
        name: str,
        description: str,
        body: str,
        tools: list[str] | None = None,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Create a NEW reusable skill (a procedure/playbook the agent will be
        offered for matching tasks). ADDITIVE-ONLY — refuses if a skill with that
        name already exists (it never overwrites; to revise an existing skill,
        propose it for review instead). Use this only for high-confidence,
        clearly-missing workflows during a `/distill` pass.

        `name` is a short label, `description` a focused one-liner (what it does /
        when to use it), `body` the procedure prompt, `tools` the tool names it
        relies on. Saved as a curator-managed skill (confidence decays if it goes
        unused), so a mistaken capture self-cleans rather than accumulating.
        """
        from runtime.state import STATE

        idx = STATE.skills_index
        if idx is None:
            return "Skills index is not available — cannot save."
        name = (name or "").strip()
        if not name:
            return "Error: skill name is required."
        if not (description or "").strip():
            return "Error: a one-line description is required (it's how the skill is matched)."
        existing = {(s.get("name") or "").strip().lower() for s in idx.all_skills()}
        if name.lower() in existing:
            return (
                f"A skill named {name!r} already exists — refusing to overwrite "
                "(additive-only). Pick a distinct name, or propose extending the "
                "existing one for review instead of auto-creating."
            )
        from graph.extensions.skills import SkillV1Artifact

        try:
            art = SkillV1Artifact(
                name=name,
                description=description.strip(),
                prompt_template=body or "",
                tools_used=list(tools or []),
                source_session_id=_session_id_from(state),
            )
        except (ValueError, TypeError) as exc:
            return f"Error building skill: {exc}"
        idx.add_skill(art, source="distilled")
        return (
            f"Created skill {name!r} (source=distilled, confidence 1.0). It'll be "
            "listed in the agent's <available_skills> index and loadable on demand "
            "via load_skill — curator-managed (confidence decays if unused)."
        )

    return [recent_activity, list_skills, save_skill]


def get_all_tools(knowledge_store=None, scheduler=None, inbox_store=None, tasks_store=None, goal_enabled=False):
    """Return every LangChain tool the lead agent + subagents can use.

    Optional dependencies:

    - ``knowledge_store`` enables the memory tools (memory_ingest,
      memory_recall, memory_list, memory_stats).
    - ``scheduler`` enables the scheduler tools (schedule_task,
      list_schedules, cancel_schedule). Accepts any backend that
      implements ``scheduler.interface.SchedulerBackend``.

    Pass ``None`` to disable either subsystem — the lead agent runs
    fine with just the four keyless general tools.
    """
    # ask_human is a lead-agent HITL tool — it pauses the A2A turn via a
    # LangGraph interrupt that only the lead turn's runner resumes. Subagents
    # (run outside that runner) must not get it, so it's gated by allowlist:
    # present in the full set for the lead agent, absent from subagent allowlists.
    # show_component is temporarily disabled (inline component rendering, ADR 0051) — see
    # issue #1323. Re-add it here (and uncomment its def above) to restore.
    tools = [current_time, calculator, web_search, fetch_url, ask_human, request_user_input, load_skill]
    # GitHub read tools (PRs/issues/commits) moved to the first-party `github`
    # plugin (opt-in) — not everyone needs them. Enable with plugins.enabled: [github].
    # Notes tools now ship with the first-party `notes` plugin (ADR 0034 S4):
    # read_note / write_note / append_note over one shared markdown doc. Enabled
    # by default (the plugin manifest), so the agent gets them without a core list here.
    # A2A federation is the `delegate_to` tool over the delegate registry (ADR
    # 0025, plugins/delegates) — it replaced the env-var `peer_consult`/`peer_list`
    # tools, which were retired (delegate_to does a2a + openai + acp behind one tool
    # with a console panel). Nothing to wire here.
    # Outbound chat-channel tools (e.g. Discord) come from their plugins (ADR
    # 0018/0019) — an installed comms plugin registers its tools when a token is set;
    # nothing to wire here.
    if knowledge_store is not None:
        tools.extend(_build_memory_tools(knowledge_store))
    if scheduler is not None:
        tools.extend(_build_scheduler_tools(scheduler))
    if inbox_store is not None:
        tools.extend(_build_inbox_tools(inbox_store))
    if tasks_store is not None:
        tools.extend(_build_task_tools(tasks_store))
    if goal_enabled:
        tools.append(_build_set_goal_tool())  # ADR 0028 — agent owns a plugin-verified goal
    # ADR 0054 — curation tools for the dream/distill subagents (read-only activity
    # + skill inventory + additive-only skill creation). Self-gate on STATE at call
    # time; present in the full set so the subagent allowlists can pick them up.
    tools.extend(_build_curation_tools())
    # Fork denylist (config ``tools.disabled``): drop named core tools without
    # editing this function. Applied last so it covers every branch above.
    if _disabled_tools:
        tools = [t for t in tools if getattr(t, "name", None) not in _disabled_tools]
    return tools


# ── deferred tools (ADR 0005 #3) ──────────────────────────────────────────────

SEARCH_TOOLS_NAME = "search_tools"

# Tools always exposed to the model when deferral is on. The keyless core +
# delegation/workflow tools + the search meta-tool itself — enough to operate
# and to *discover* the rest. Everything else is deferred until searched.
DEFERRED_BASE_TOOL_NAMES = frozenset(
    {
        "current_time",
        "calculator",
        "web_search",
        "fetch_url",
        "ask_human",
        "request_user_input",
        "load_skill",
        "task",
        "task_batch",
        "run_workflow",
        "save_workflow",
        SEARCH_TOOLS_NAME,
    }
)


def resolve_deferred_keep(configured_keep) -> set[str]:
    """Resolve the always-on tool set for deferral: the configured override (if
    any) else the built-in base. ``search_tools`` is always kept — without it the
    agent could never load anything back."""
    keep = {str(n) for n in (configured_keep or [])} or set(DEFERRED_BASE_TOOL_NAMES)
    keep.add(SEARCH_TOOLS_NAME)
    return keep


def _tool_summary(t) -> str:
    """First non-empty line of a tool's description, truncated."""
    desc = (getattr(t, "description", "") or "").strip()
    first = next((ln.strip() for ln in desc.splitlines() if ln.strip()), "")
    return (first[:119].rstrip() + "…") if len(first) > 120 else first


def build_search_tools_tool(all_tools, keep_names):
    """Build the ``search_tools`` meta-tool over the *deferred* tools.

    It keyword-matches the deferred tools (everything not in ``keep_names``) by
    name + description and returns matches as a backticked bulleted list. The
    ``ToolDeferralMiddleware`` reads those backticked names from the result and
    binds the matched tools on subsequent turns (progressive disclosure).
    """
    keep = set(keep_names)
    catalog = [(t.name, _tool_summary(t)) for t in all_tools if getattr(t, "name", None) and t.name not in keep]

    def _render(pairs, header) -> str:
        lines = [header]
        for name, summary in pairs:
            lines.append(f"- `{name}` — {summary}" if summary else f"- `{name}`")
        return "\n".join(lines)

    @tool
    def search_tools(query: str = "", limit: int = 10) -> str:
        """Find and load additional tools by capability.

        Most tools are not shown up-front, to keep your working context focused.
        When your visible tools don't cover the task, call this with a few
        keywords describing what you need (e.g. "github pull request", "schedule
        reminder", "read notes panel"). Matching tools become available to call
        on your next step. Leave ``query`` empty to list every available tool.
        Returns a bulleted list of ``name — purpose``.
        """
        if not catalog:
            return "No additional tools are available beyond the ones already shown."
        terms = (query or "").lower().split()
        lim = max(1, min(int(limit or 10), 50))
        if not terms:
            return _render(catalog[:lim], "All additional tools — now available to call:")
        scored = []
        for name, summary in catalog:
            hay = f"{name} {summary}".lower()
            score = sum(hay.count(term) for term in terms)
            if score:
                scored.append((score, name, summary))
        if not scored:
            return _render(
                catalog[:lim],
                f'No tool matched "{query}". Here are the available tools (now callable):',
            )
        scored.sort(key=lambda r: (-r[0], r[1]))
        shown = [(n, s) for _, n, s in scored[:lim]]
        return _render(shown, f'Found {len(shown)} tool(s) for "{query}" — now available to call:')

    return search_tools
