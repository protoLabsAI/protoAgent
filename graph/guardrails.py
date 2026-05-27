"""RAG guardrails — relevance grading + query rewriting.

Two retrieval-quality primitives backported from the protoLabs fleet
(protoResearcher/quinn ``guardrails.py``), generalised so core doesn't hardcode
a gateway/model/token: the LLM call is a **pluggable callable** the fork
supplies. Both fail open — an LLM outage degrades quality, never availability.

- ``grade_document`` — binary "is this chunk relevant to the query?" check
  before the agent leans on a retrieved doc. Pluggable ``grade_fn(query,
  excerpt) -> bool | str``.
- ``rewrite_query`` — rewrite a sparse-result query for a better second pass.
  Pluggable ``rewrite_fn(query) -> str``.

Pairs with ``cache.ResponseCache`` (memoise the grade/rewrite calls).

    from graph.guardrails import grade_document, filter_relevant, rewrite_query
    kept = await filter_relevant(q, hits, grade_fn=my_grader, key=lambda h: h["preview"])
"""

from __future__ import annotations

import inspect
import logging
from typing import Awaitable, Callable, Union

log = logging.getLogger(__name__)

GRADE_PROMPT = (
    "Is the following content relevant to answering the query? "
    "Answer only 'yes' or 'no'.\n\nQuery: {query}\n\nContent:\n{excerpt}"
)
REWRITE_PROMPT = (
    "Rewrite this search query to retrieve better results — expand acronyms, "
    "add synonyms, keep it concise. Return only the rewritten query.\n\n{query}"
)

_MIN_CONTENT_CHARS = 50
_EXCERPT_CHARS = 500

GradeFn = Callable[[str, str], Union[bool, str, "Awaitable[bool | str]"]]
RewriteFn = Callable[[str], Union[str, "Awaitable[str]"]]


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _is_yes(answer) -> bool:
    if isinstance(answer, bool):
        return answer
    return str(answer).strip().lower().startswith("y")


async def grade_document(query: str, content: str, *, grade_fn: GradeFn) -> bool:
    """Return whether *content* is relevant to *query*.

    Very short content is rejected outright. Otherwise ``grade_fn`` is asked
    (sync or async); any error **fails open** (returns True) so an LLM outage
    never silently drops every document.
    """
    if not content or len(content.strip()) < _MIN_CONTENT_CHARS:
        return False
    try:
        answer = await _maybe_await(grade_fn(query, content[:_EXCERPT_CHARS]))
        return _is_yes(answer)
    except Exception as exc:  # noqa: BLE001 - fail open
        log.debug("[guardrails] grade_fn failed, keeping doc: %s", exc)
        return True


async def filter_relevant(query, docs, *, grade_fn: GradeFn, key=None) -> list:
    """Keep only the docs ``grade_document`` rates relevant to *query*.

    ``key`` extracts the text to grade from each doc (default: the doc itself).
    """
    extract = key or (lambda d: d)
    kept = []
    for d in docs:
        if await grade_document(query, str(extract(d)), grade_fn=grade_fn):
            kept.append(d)
    return kept


async def rewrite_query(query: str, *, rewrite_fn: RewriteFn) -> str:
    """Return a rewritten *query* (sync or async ``rewrite_fn``).

    Falls back to the original query on empty output or error.
    """
    try:
        rewritten = await _maybe_await(rewrite_fn(query))
        rewritten = (rewritten or "").strip()
        return rewritten or query
    except Exception as exc:  # noqa: BLE001 - fall back to original
        log.debug("[guardrails] rewrite_fn failed, using original: %s", exc)
        return query
