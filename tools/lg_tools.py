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
- ``session_search`` — search prior session transcripts by content
- ``recall_session`` — expand one <prior_sessions> digest entry in full
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
import logging
import operator as _op
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

from tools.fallbacks import with_fallback
from tools.goal_tools import (  # re-exported: tests / plugins / forks import them from here
    _build_abandon_goal_tool,
    _build_goal_plan_tool,
    _build_list_verifiers_tool,
    _build_set_goal_tool,
)
from tools.memory_tools import (  # re-exported: tests / plugins / forks import them from here
    _build_memory_tools,
    _memory_citation,  # noqa: F401 — re-export only (tests/test_memory_provenance.py)
)
from tools.scheduler_tools import (  # re-exported: tests / plugins / forks import them from here
    _build_scheduler_tools,
    _build_task_tools,
    _build_watch_tools,
    _humanize_duration,  # noqa: F401 — re-export only (forks / plugins import it from here)
)
from tools.self_edit_tools import (  # re-exported: tests / plugins / forks / operator MCP import them from here
    _CONFIG_WRITE_DENIED,  # noqa: F401 — re-export only
    _CONFIG_WRITE_DENIED_LEAVES,  # noqa: F401 — re-export only
    _SOUL_MAX_BYTES,  # noqa: F401 — re-export only
    _apply_soul_section_edit,  # noqa: F401 — re-export only (tests/test_edit_soul_tool.py)
    _build_config_editor_tool,
    _build_curation_tools,
    _build_fleet_diagnostics_tool,
    _build_skill_editor_tools,
    _build_soul_editor_tool,
    _config_write_refusal,  # noqa: F401 — re-export only
    _publish_persona_event,  # noqa: F401 — re-export only
)
from tools.session import _session_id_from  # noqa: F401 — re-exported: forks / plugins import it from here

log = logging.getLogger("protoagent.tools")


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

    Autonomous turns (scheduled / inbox / background) have no operator watching the
    chat, so don't block on a human here — prefer proceeding with your best judgment
    and stating the assumption. (If you do ask on such a turn, the runtime
    auto-answers it so the turn can't deadlock.)
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
    pauses (surfaced as ``input-required``) until they submit; their response (the
    submitted fields, as a JSON object) is returned from this call.

    ``steps`` is a list of form steps. Multiple steps render as a sequential
    **wizard** (one step per screen with Back / Next and a progress indicator); the
    operator advances through them and the final step submits every step's answers
    together as one object. Use several steps to pace a longer series of questions;
    use one step for a simple form. Each step is ``{"schema": <JSON Schema draft-07
    of the step's fields>, "title"?: str, "description"?: str}`` — supply at least
    one step that defines fields.

    Field types (per property in a step's ``schema.properties``):
    - text / number / boolean → ``{"type": "string"|"number"|"integer"|"boolean"}``;
      add ``"format": "textarea"`` for multi-line text.
    - **single-choice cards** (preferred for "pick one of these") →
      ``{"type": "string", "oneOf": [{"const": "pg", "title": "Postgres",
      "description": "Durable, multi-writer"}, {"const": "sqlite", "title": "SQLite",
      "description": "Zero-config"}]}``. Each option shows its label + description as
      a selectable card. (A bare ``"enum": [...]`` renders as a plain dropdown.)
    - **multi-choice cards** ("pick any") → wrap the same options in an array:
      ``{"type": "array", "items": {"oneOf": [...]}}`` → the value is a list.
    Mark a field required via the step schema's ``"required": [...]``; required
    fields gate Next / Submit. A field's ``"default"`` prefills its answer — the
    form opens with it filled in (choice cards render it selected) and a default
    **counts as an answer**, so a required-with-default field doesn't gate: the
    operator can submit your proposal untouched. Use defaults to propose values
    you want confirmed or adjusted, not for fields they must actively decide.

    Phrase the ask clearly and only request fields you actually need. For a single
    free-text or yes/no question, use ``ask_human`` instead. As with ``ask_human``,
    don't block on this in an autonomous turn — there may be no operator to submit
    the form (the runtime auto-answers it there).
    """
    import json
    from langgraph.types import interrupt

    # A form with no steps renders as a bare free-text box (the console treats zero-step
    # payloads as an ask_human prompt), silently dropping the structured contract — guide
    # the model back instead of degrading. The LLM reads this and retries.
    if not steps:
        return (
            "Error: request_user_input needs at least one form step. Pass "
            'steps=[{"schema": {"type": "object", "properties": {…}, "required": […]}}], '
            "or use ask_human for a single free-text or yes/no question."
        )

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


@tool
def show_component(component: str, props: dict, title: str = "") -> str:
    """Render structured data as a typed widget INLINE in the chat (ADR 0051).

    Pick this over a markdown blob when the data fits a curated shape — a comparison
    ``table``, a ``keyvalue`` status/metrics block, or a ``timeline`` of steps. It renders
    inline in your answer, data-only and safe (no sandbox).

    For free-form or custom-rendered visuals — a chart, a Mermaid diagram, bespoke
    HTML/React/SVG — use ``show_artifact`` instead (it renders generated CODE in a separate
    sandboxed panel). Rule of thumb: a data SHAPE (table / metrics / steps) → this tool;
    a generated VISUAL → an artifact.

    Args:
        component: one of ``"table"``, ``"keyvalue"``, ``"timeline"``.
        props: the component's data:
            - table:    ``{"columns": ["A","B"], "rows": [["a1","b1"], ...]}``
            - keyvalue: ``{"items": [{"label": "Credits", "value": "183k"}, ...]}``
            - timeline: ``{"steps": [{"label": "Buy hauler", "state": "done|active|todo",
                          "detail": "…"}, ...]}``
        title: optional heading shown above the component.

    Renders immediately for the user; also briefly summarize the data in your text reply
    (the component is a visual aid, not a substitute for your answer).
    """
    from graph.components import COMPONENT_TYPES, encode_component

    if component == "code-ref":
        # A code-ref is a pointer into a fenced file; only show_code (ADR 0112) validates
        # the fence, the secret deny-list and the line range before emitting one.
        return "Error: use the show_code tool to point the operator at code (bound when the code pane is on)."
    if component not in COMPONENT_TYPES:
        return f"Error: unknown component '{component}'. Use one of: {', '.join(t for t in COMPONENT_TYPES if t != 'code-ref')}."
    payload = dict(props or {})
    if title and "title" not in payload:
        payload["title"] = title
    return f"Rendered a {component} component for the user. " + encode_component(component, payload)


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
        # Two distinct failures share this exception: an unknown NAME, and a host
        # with NO IANA database at all — Windows ships none (stdlib zoneinfo needs
        # the bundled `tzdata` package there, #1683) and slim containers often lack
        # /usr/share/zoneinfo. On a database-less host every name fails — including
        # this docstring's examples — so answer in UTC rather than erroring the
        # tool's core job ("what time is it").
        try:
            ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            now = datetime.now(UTC)
            note = (
                ""
                if timezone.strip().upper() in ("UTC", "ETC/UTC")
                else f"\nNote: no IANA timezone database on this host — {timezone!r} unavailable, showing UTC."
            )
            return f"{now.isoformat()} (UTC)\nHuman: {now.strftime('%A, %B %d %Y, %H:%M:%S %Z')}{note}"
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

    # ddgs.text() is a SYNCHRONOUS network call — running it inline would block the asyncio
    # event loop for the whole search. Under a fan-out (e.g. task_batch's parallel
    # researchers) those blocking searches serialise on the loop and starve everything else,
    # incl. the cancel/tasks API. Offload to a worker thread so the loop stays responsive.
    def _run_search() -> list:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    try:
        results = await asyncio.to_thread(_run_search)
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

    # Out of hops while still redirecting: the body is a 30x stub, not the page — say so
    # rather than hand it to the model as the page's content under a `[302]` header.
    if resp.is_redirect:
        return f"Error: too many redirects (more than {hops}) fetching {url}"

    if resp.status_code >= 400:
        return f"Error: HTTP {resp.status_code} for {url}"

    content = resp.content[:_MAX_FETCH_BYTES]
    ctype = (resp.headers.get("content-type") or "").lower()

    if "html" in ctype or content.lstrip().startswith(b"<"):
        # BeautifulSoup parsing of up to _MAX_FETCH_BYTES (2MB) is CPU-heavy and synchronous;
        # inline it would block the event loop per fetch. Offload to a thread so concurrent
        # fetches (and the rest of the server) keep making progress.
        text = await asyncio.to_thread(_extract_text_from_html, content)
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

    # <header> is dual-use: page banners are chrome, but article/section headers
    # hold the title and byline. Decide per-header BEFORE the generic <nav> strip
    # below — "wraps a <nav>" is the strongest chrome signal and would be invisible
    # afterwards. A header inside sectioning content (main/article/section/aside)
    # is content and survives. On pages WITHOUT <main>/<article> (div-soup pages),
    # only banner-position headers (direct children of <body>/<html>) and
    # nav-wrapping headers are chrome — a header nested deeper is presumed to be
    # an article header (title/byline) and is kept.
    has_semantic = soup.find(["main", "article"]) is not None
    page_top = [t for t in (soup, soup.html, soup.body) if t is not None]
    for el in soup.find_all("header"):
        if el.decomposed:
            continue
        if el.find_parent(["main", "article", "section", "aside"]) is not None:
            continue
        if has_semantic or any(el.parent is t for t in page_top) or el.find("nav") is not None:
            el.decompose()

    for el in soup(["script", "style", "nav", "footer", "noscript"]):
        el.decompose()

    # ARIA landmark chrome — explicit roles mark nav/banner/footer regions even
    # when the page is all <div>s (Reddit-style).
    for role in ("navigation", "banner", "contentinfo"):
        for el in soup.find_all(attrs={"role": role}):
            if not el.decomposed:
                el.decompose()

    # Widget chrome by exact class token — bs4 matches whole tokens, so
    # class="sidebar" is stripped while class="sidebar-open" survives.
    for cls in ("sidebar", "menu", "breadcrumb"):
        for el in soup.find_all(class_=cls):
            if not el.decomposed:
                el.decompose()

    # Prefer <main> / <article> when the page uses them, then an explicit
    # role="main"; otherwise the dominant <div> (readability-lite) before body.
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or _dominant_content_node(soup.body or soup)
    )
    lines = [line.strip() for line in main.get_text("\n").splitlines() if line.strip()]
    return "\n".join(lines)


def _dominant_content_node(node):
    """Readability-lite fallback for pages without <main>/<article>/role="main":
    starting at <body>, descend into the single child <div> holding the majority
    of the text, and return the tightest such container. A step that would drop a
    kept <header> or <h1> (the article title/byline on wrapper-less pages) stops
    the descent instead — narrowing must never cost content, only chrome."""
    while True:
        total = len(node.get_text())
        if not total:
            return node
        nxt = None
        for child in node.find_all("div", recursive=False):
            if 2 * len(child.get_text()) > total:
                nxt = child
                break
        if nxt is None:
            return node
        if any(len(nxt.find_all(t)) != len(node.find_all(t)) for t in ("header", "h1")):
            return node
        node = nxt


# ── memory tools ─────────────────────────────────────────────────────────────
#
# The memory/knowledge tool cluster lives in ``tools/memory_tools.py`` (#3820);
# ``_build_memory_tools`` is re-exported above and bound in ``get_all_tools``.


# Operator/fork tool denylist — names dropped from the agent's toolset. Set once from
# config (``tools.disabled``) by ``set_disabled_tools`` at config load/reload, so an
# operator removes tools via YAML/Settings instead of editing core (an edit that would
# conflict on every upstream re-sync). Applied inside ``get_all_tools`` AND — via
# ``drop_disabled_tools`` — over the fully assembled set in ``graph.agent`` (so it also
# covers plugin/MCP ``extra_tools``, the delegation tools, the filesystem tools incl.
# ``run_command``, and late-seam tools). Plugins still ADD tools.
_disabled_tools: set[str] = set()


def set_disabled_tools(names) -> None:
    """Set the operator tool denylist (config ``tools.disabled``)."""
    global _disabled_tools
    _disabled_tools = {str(n).strip() for n in (names or []) if str(n).strip()}


def drop_disabled_tools(tools: list, dropped: list | None = None) -> list:
    """Filter the denylist (``tools.disabled``) out of ``tools`` — the single filter
    every assembly point uses, so a disabled name is gone no matter which seam
    contributed it. Returns ``tools`` unchanged (same list) when the denylist is empty.

    ``dropped`` (optional) collects the tool objects that were filtered out, so the
    graph builder can stamp a full catalog (bound + disabled) for the operator Tools
    tab — a toggled-off tool must stay visible there or it could never be re-enabled."""
    if not _disabled_tools:
        return tools
    kept = []
    for t in tools:
        if getattr(t, "name", None) in _disabled_tools:
            if dropped is not None:
                dropped.append(t)
        else:
            kept.append(t)
    return kept


# Stable list of scheduler tool names. Exposed as a module-level
# constant so ``graph/config_io.py::list_available_tools`` can show
# the wizard the right surface even when the runtime hasn't yet
# constructed a scheduler instance (e.g. fresh boot before setup is
# complete). Keep in sync with ``tools/scheduler_tools.py::_build_scheduler_tools``.
SCHEDULER_TOOL_NAMES: tuple[str, ...] = (
    "schedule_task",
    "list_schedules",
    "cancel_schedule",
)
MEMORY_TOOL_NAMES: tuple[str, ...] = (
    "memory_ingest",
    "knowledge_ingest",
    "memory_recall",
    "session_search",
    "recall_session",
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


# ── registry ─────────────────────────────────────────────────────────────────


def _config_gated_tool_reasons(config) -> dict[str, str]:
    """The small, EXPLICIT set of host-config gates that guarantee a named tool is
    absent or would refuse, evaluated against the live ``config`` — tool name → reason.

    Only UNCONDITIONAL gates belong here: a disabled subsystem or an empty allowlist
    that refuses regardless of arguments. Per-call/network refusals (a bad URL, an
    offline remote) are NOT guarantees and must never be reported as one. Keyed by
    tool name so ``load_skill`` can turn a bare "not bound" into an actionable reason.
    """
    reasons: dict[str, str] = {}
    if config is None:
        return reasons
    # Project onboarding (#2555): ``onboarding.enabled: false`` makes
    # ``build_onboard_tools`` return ``[]`` — the registration tool is removed from the
    # toolset entirely rather than bound-and-refusing. Name the config, not just the
    # absence, so the agent can tell the operator what to turn on. Both the current
    # tool name and the reported historical alias (see ``graph/tool_delta``) are covered.
    if not getattr(config, "onboarding_enabled", True):
        reason = "project onboarding is disabled — set onboarding.enabled to bind it"
        for tool_name in ("board_register_project", "onboard_project", "register_local_project"):
            reasons[tool_name] = reason
    return reasons


#: Cap on tools listed in the load_skill unavailable annotation — keeps the note bounded
#: even if a skill declares an unusually long advisory tool list.
_MAX_UNAVAILABLE_LISTED = 20


def _invocation_bound_tool_names() -> frozenset[str] | None:
    """The tool names bound to the graph EXECUTING this call, or ``None`` when no graph
    is committed (boot, or the operator-MCP skills path, where there is no running graph).

    Reads ``STATE.graph.bound_tools`` — the set ``create_agent_graph`` stamps on the
    compiled graph as the single source of truth for "what tools the model has" (derived
    from the real assembled surface after every filter/append/deferral pass), the same
    attribute ``/api/tools`` and the capability-contract audit read. This is deliberately
    NOT ``tool_delta.current_toolset``: that module global is re-recorded by EVERY graph
    build in the process — a cache-warmer build, a ``create_simple_agent``, a test graph,
    an in-progress reload — so a build that never becomes the running graph could overwrite
    the set another invocation reconciles against. The committed ``STATE.graph`` is the one
    this tool call actually runs inside, so its ``bound_tools`` is the invocation's own
    surface. Read-only and error-safe: any surprise degrades to ``None`` (no absence claim)."""
    try:
        from runtime.state import STATE

        bound = getattr(STATE.graph, "bound_tools", None)
        if bound is None:
            return None
        # Subagent-only tools (ADR 0117) are not the lead's, but the subagent that owns them
        # loads its skills through this same graph state, so they are not PROVEN absent for
        # this caller: never annotate them as unavailable.
        held = getattr(STATE.graph, "subagent_only_tools", None) or []
        # ``bound_tools`` is a list of tool OBJECTS (each with ``.name``); mirror the
        # reader in ``server/agent_init`` so a stray non-tool entry can't raise here.
        return frozenset(getattr(t, "name", None) or str(t) for t in [*bound, *held])
    except Exception:  # noqa: BLE001 — advisory annotation must never break a skill load
        return None


def _skill_tools_unavailable_note(tools_used) -> str:
    """Reconcile a skill's advisory ``Relevant tools`` against what is ACTUALLY reachable
    in this invocation, returning a one-line ``Unavailable in this context:`` annotation
    (or ``""`` when everything declared is reachable).

    Two sources, both authoritative — never a duplicated static inventory:

    - the tools bound to the graph executing this call (``STATE.graph.bound_tools`` via
      ``_invocation_bound_tool_names``), used to report PROVEN absence — a tool the model
      simply does not have; and
    - the small explicit set of host-config gates (``_config_gated_tool_reasons``) that
      guarantee a named tool refuses, used to name the *reason* (e.g. onboarding disabled).

    Deliberately conservative: with no committed graph (nothing built yet) we make no
    absence claim, and we never predict runtime/network failure. Error-safe (never
    raises) and bounded (``_MAX_UNAVAILABLE_LISTED``)."""
    names = [n for n in ((tools_used or []) if not isinstance(tools_used, str) else tools_used.split())]
    if not names:
        return ""
    bound = _invocation_bound_tool_names()
    try:
        from runtime.state import STATE

        gated = _config_gated_tool_reasons(STATE.graph_config)
    except Exception:  # noqa: BLE001
        gated = {}

    entries: list[str] = []
    seen: set[str] = set()
    for raw in names:
        tool_name = (raw or "").strip()
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        # A known fail-closed config gate is the most specific, actionable reason —
        # prefer it over a bare "not bound" even though the gate also unbinds the tool.
        if tool_name in gated:
            entries.append(f"{tool_name} ({gated[tool_name]})")
        # Otherwise report ONLY proven absence: without an authoritative bound set we
        # cannot say a tool is missing, and the card forbids guessing.
        elif bound is not None and tool_name not in bound:
            entries.append(f"{tool_name} (not bound in this context)")

    if not entries:
        return ""
    shown = entries[: _MAX_UNAVAILABLE_LISTED]
    more = f" (+{len(entries) - len(shown)} more)" if len(entries) > len(shown) else ""
    return f"Unavailable in this context: {', '.join(shown)}{more}."


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

    The skill's advisory ``Relevant tools`` are reconciled up front against the tools
    actually bound to the graph executing this call (``STATE.graph.bound_tools``) plus
    the known host-config gates; any that are missing or configuration-refused are called
    out under ``Unavailable in this context:`` before the procedure. The annotation is
    advisory — the full body still loads (progressive disclosure is preserved).
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
        note = _skill_tools_unavailable_note(tools_used)
        if note:
            lines.append(note)
    lines.append(f"\n## Procedure\n{body}" if body else "\n(This skill has no recorded procedure.)")
    return "\n".join(lines)


# ── self-editing tools ────────────────────────────────────────────────────────
#
# Curation / skill editor / SOUL editor / config editor / fleet-diagnostics builders live
# in ``tools/self_edit_tools.py`` (#3839); they are re-exported above and bound in
# ``get_all_tools``.


# Lead-only HITL tools: each pauses the lead A2A turn via a LangGraph interrupt that only the
# lead turn's runner resumes. A subagent runs on a checkpointer-less graph, so binding one
# would fail opaquely mid-delegation — graph.agent hard-denies these from every subagent's
# tool set (``_subagent_tools``) regardless of its allowlist. Keep this in sync if a new
# interrupt-based tool is added.
HITL_TOOL_NAMES = frozenset({"ask_human", "request_user_input"})


def get_all_tools(
    knowledge_store=None,
    scheduler=None,
    inbox_store=None,
    tasks_store=None,
    goal_enabled=False,
    watches_enabled=False,
    graph_config=None,
    background_mgr=None,
    dropped=None,
    soul_edit_enabled=False,
    self_config_enabled=False,
    skill_edit_enabled=False,
    self_improvement_provenance_required=False,
    reload_callback=None,
):
    """Return every LangChain tool the lead agent + subagents can use.

    Optional dependencies:

    - ``knowledge_store`` enables the memory tools (memory_ingest,
      knowledge_ingest, memory_recall, session_search, recall_session, memory_list,
      memory_stats).
    - ``scheduler`` enables the scheduler tools (schedule_task,
      list_schedules, cancel_schedule). Accepts any backend that
      implements ``scheduler.interface.SchedulerBackend``.
    - ``graph_config`` (the ``LangGraphConfig``) lets ``knowledge_ingest``
      build the gateway STT/vision functions for audio/video/image sources.
      Optional — without it, only the text/URL/PDF/YouTube ingest paths work.
    - ``background_mgr`` (ADR 0050) lets ``knowledge_ingest`` run a slow
      source (URL fetch / media transcription) as a detached background job
      instead of blocking the turn. Optional — without it it ingests inline.
    - ``self_config_enabled`` binds the guarded ``set_config`` tool (operational keys
      only; the trust surface is refused). Lead-agent only, like ``edit_soul``.
    - ``soul_edit_enabled`` (ADR 0079/0081) binds the guarded ``edit_soul`` tool
      — the lead agent's self-authored persona editor. Default off; lead-only
      (no subagent build passes it). ``reload_callback`` is the server-owned
      graph reload, INJECTED (not imported) and handed to ``edit_soul`` so a
      persona self-edit goes live on the next turn without ``tools/`` reaching
      into ``server/``. ``None`` degrades to next-natural-reload semantics.
    - ``skill_edit_enabled`` binds guarded skill update/delete tools. Each mutation
      archives the outgoing skill for rollback before changing the live artifact.

    Pass ``None`` to disable either subsystem — the lead agent runs
    fine with just the four keyless general tools.
    """
    # ask_human AND request_user_input are lead-agent HITL tools (HITL_TOOL_NAMES) — each
    # pauses the A2A turn via a LangGraph interrupt that only the lead turn's runner resumes.
    # Subagents (run outside that runner, on a graph with no checkpointer) must not get either:
    # they're absent from every subagent allowlist in graph/subagents/config.py AND hard-denied
    # in graph.agent._subagent_tools, so a fork can't enable one via a tools list either.
    # show_component (inline component rendering, ADR 0051 / #1323): table/keyvalue/timeline
    # widgets rendered inline in chat by the console's extensible component registry.
    tools = [current_time, calculator, web_search, fetch_url, ask_human, request_user_input, load_skill, show_component]
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
    if graph_config is not None:
        # Read-only self-config introspection (#2540). An agent could see neither its
        # own YAML (outside every fs fence) nor any merged view of it, so a
        # misconfiguration was indistinguishable from a bug — protoEngineer burned two
        # sessions on a board bound to the wrong repo. Enabled wherever a config is
        # available; `tools.disabled` turns it off for an instance that doesn't want it.
        from tools.config_tools import build_config_tools

        tools.extend(build_config_tools(graph_config))
        # Project onboarding (#2555) — bounded clone (`onboard_project`) + local
        # registration (`register_local_project`) within the operator-consented
        # `onboarding` space. Absent unless `onboarding.enabled`;
        # the factory returns [] when off, so a non-opted-in instance gets no
        # onboarding surface at all rather than a tool that only refuses.
        from tools.onboard_tools import build_onboard_tools

        tools.extend(build_onboard_tools(graph_config))
    if knowledge_store is not None:
        tools.extend(_build_memory_tools(knowledge_store, graph_config, background_mgr))
    if scheduler is not None:
        tools.extend(_build_scheduler_tools(scheduler))
    if inbox_store is not None:
        tools.extend(_build_inbox_tools(inbox_store))
    if tasks_store is not None:
        tools.extend(_build_task_tools(tasks_store))
    from graph.goals.verifiers import plugin_verifier_names

    _has_verifiers = bool(plugin_verifier_names())
    if goal_enabled or watches_enabled:
        tools.append(_build_list_verifiers_tool())  # discover registered verifiers before set_goal/create_watch
    if goal_enabled and _has_verifiers:
        tools.append(_build_set_goal_tool())  # ADR 0028 — agent owns a plugin-verified goal
        tools.append(_build_goal_plan_tool())  # goal loop — record running plan (retired <goal_plan>)
        tools.append(_build_abandon_goal_tool())  # goal loop — explicit give-up (retired <goal_unachievable>)
    if watches_enabled and _has_verifiers:
        # ADR 0067 — many concurrent supervised watches, gated by `watches.enabled` ALONE.
        # A watch is not a goal: it's verifier-only, keyed by its own id, and moved by an
        # external process, so binding it inside the goal-enabled group (as it was until
        # this change) coupled two unrelated dispositions — an instance that turned goal
        # mode off lost the watch tools it never asked to lose. Tool availability ONLY:
        # stored watches and the background poller are independent of both flags.
        tools.extend(_build_watch_tools())
    if self_config_enabled:
        # Guarded agent-owned config writes (config tools.self_config_enabled, default
        # off). Lead-agent only — no subagent build passes the flag, so a bounded subagent
        # can never reach the write path. The fence lives in the tool, not here, so the
        # refusal is legible to the model that tried it.
        tools.extend(_build_config_editor_tool())
    if getattr(graph_config, "tools_fleet_diagnostics_enabled", False):
        # Fleet diagnostics (ADR 0071 / #3170), default off. The adapter exposes only
        # read-only member logs and exact task-by-id inspection through the existing
        # authenticated fleet proxy; no operator/runtime mutation path is bound here.
        tools.extend(_build_fleet_diagnostics_tool())
    if soul_edit_enabled:
        # ADR 0079/0081 — guarded self-authored persona (config soul.self_edit_enabled,
        # default off). Lead-agent only: no subagent build passes soul_edit_enabled, so
        # edit_soul never reaches a bounded subagent. reload_callback (server-injected)
        # makes the edit live on the next turn without tools/ importing server/.
        tools.extend(
            _build_soul_editor_tool(
                reload_callback,
                provenance_required=self_improvement_provenance_required,
            )
        )
    if skill_edit_enabled:
        tools.extend(_build_skill_editor_tools(provenance_required=self_improvement_provenance_required))
    # ADR 0054 — curation tools for the dream/distill subagents (read-only activity
    # + skill inventory + additive-only skill creation). Self-gate on STATE at call
    # time; present in the full set so the subagent allowlists can pick them up.
    tools.extend(_build_curation_tools(provenance_required=self_improvement_provenance_required))
    # Operator denylist (config ``tools.disabled``): drop named core tools without
    # editing this function. Applied last so it covers every branch above. (graph.agent
    # re-applies it over the FULL assembled set — extra/fs/late tools — post-assembly.
    # ``dropped`` collects the filtered-out tools for the operator catalog.)
    return drop_disabled_tools(tools, dropped)


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


def build_search_tools_tool(all_tools, keep_names, held_names=()):
    """Build the ``search_tools`` meta-tool over the *deferred* tools.

    It keyword-matches the deferred tools (everything not in ``keep_names``) by
    name + description and returns matches as a backticked bulleted list. The
    ``ToolDeferralMiddleware`` reads those backticked names from the result and
    binds the matched tools on subsequent turns (progressive disclosure).

    ``held_names`` are ``tools.subagent_only`` tools (ADR 0117): listed only to a call
    running under a turn fence that names them (a background subagent on the lead graph),
    never to the lead, so deferral can still load them for the subagent that owns them.
    """
    from graph.fence_scope import current_fence

    keep = set(keep_names)
    held = set(held_names)
    full_catalog = [(t.name, _tool_summary(t)) for t in all_tools if getattr(t, "name", None) and t.name not in keep]

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
        fence = set(current_fence())
        catalog = [(n, s) for n, s in full_catalog if n not in held or n in fence]
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
