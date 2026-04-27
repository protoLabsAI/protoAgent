# Starter tools

Nine tools ship in `tools/lg_tools.py`:

- Four keyless general-purpose tools — `current_time`, `calculator`, `web_search`, `fetch_url` — that work without any state.
- Five **memory tools** — `memory_ingest`, `memory_recall`, `memory_list`, `memory_stats`, `daily_log` — bound to the bundled `KnowledgeStore` (sqlite + FTS5, see [Configuration](/reference/configuration#knowledge)).

`get_all_tools(knowledge_store)` is the registry. When `knowledge_store` is `None` (the store is disabled in config) the memory tools are omitted automatically.

## `current_time`

```python
@tool
async def current_time(timezone: str = "UTC") -> str
```

Returns the current wall-clock time in the given IANA timezone (e.g. `"UTC"`, `"America/New_York"`, `"Asia/Tokyo"`). Defaults to UTC.

Output:

```
2026-04-17T13:23:42.644606-04:00 (America/New_York)
Human: Friday, April 17 2026, 13:23:42 EDT
```

Unknown timezones return `"Error: unknown timezone 'Not/A_Zone'. ..."` — never raises.

## `calculator`

```python
@tool
async def calculator(expression: str) -> str
```

Safely evaluates a numeric expression using AST parsing. **Does not call `eval()`**.

Supported:

| Op | Example |
|---|---|
| `+ - * /` | `1 + 2 * 3` |
| `//` floor div | `10 // 3` |
| `%` mod | `10 % 3` |
| `**` power | `2 ** 10` |
| Unary `-` | `-5 + 3` |
| Parens | `(1 + 2) * 3` |

Rejected (returns error string):

- Names (`__import__`, any identifier)
- Function calls (`abs(-5)`)
- Attribute access (`(1).__class__`)
- Anything that's not pure arithmetic

Output on success: `"2 ** 10 = 1024"`. Division by zero returns `"Error: division by zero"`.

## `web_search`

```python
@tool
async def web_search(query: str, max_results: int = 5) -> str
```

DuckDuckGo text search via the `ddgs` package. No API key. `max_results` is clamped to 1–10.

Output:

```
3 result(s) for 'LangGraph tutorial':
1. LangGraph Introduction — https://langchain.com/langgraph
   LangGraph is a framework for building...
2. ...
```

Failures (network, rate-limit, import error) return `"Error: ..."` strings. The LLM reads the error and retries or degrades gracefully.

## `fetch_url`

```python
@tool
async def fetch_url(url: str, max_chars: int = 8000) -> str
```

Fetches a URL and returns cleaned plain-text content.

Guarantees:

- URL scheme must be `http://` or `https://`. `file://`, `javascript:`, `ftp://`, etc. are rejected.
- Response body is capped at 2MB before parsing (blast-radius cap).
- Text output is truncated at `max_chars` with `…[truncated]` marker.
- HTML pages: scripts, styles, nav, footer, noscript are stripped. Prefers `<main>` / `<article>` over the full body.
- Non-HTML content (JSON, plain text, CSV) is decoded and returned as-is.

User-Agent is `protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)`. Customize in the tool body if your fork hits rate-limited APIs that need something specific.

Output:

```
[200] https://example.com

Example Domain
This domain is for use in documentation examples...
```

## `memory_ingest`

```python
@tool
async def memory_ingest(content: str, domain: str = "general", heading: str | None = None) -> str
```

Stores a chunk in the bundled `KnowledgeStore`. Use for things the operator wants you to remember across sessions — preferences, environment facts, decisions worth recalling later.

`domain` is a logical bucket (`"preferences"`, `"context"`, `"general"`, …). `heading` is an optional short label that doubles as a stable de-dupe key.

Returns `"Stored chunk 17 in 'preferences'."` on success, an error string when the store is unavailable.

## `memory_recall`

```python
@tool
async def memory_recall(query: str, k: int = 5) -> str
```

Top-k keyword search over the store via FTS5 (LIKE fallback). Returns one match per line:

```
[preferences] coffee: Operator's preferred coffee is a Gibraltar with oat milk.
[context] lab: Primary lab is Snickerdoodle in Spokane.
```

Returns `"No matches."` when nothing scores above the keyword threshold.

## `memory_list`

```python
@tool
async def memory_list(domain: str | None = None, limit: int = 10) -> str
```

Most-recent-first listing of stored chunks. Filter by domain when given. Useful for "what did I log today?" style queries.

## `memory_stats`

```python
@tool
async def memory_stats() -> str
```

Per-domain chunk counts plus a total. Useful for sanity-checking that ingest landed.

## `daily_log`

```python
@tool
async def daily_log(content: str) -> str
```

Convenience wrapper around `memory_ingest` that writes to `domain='daily-log'` with today's UTC date as the heading. Same-day entries cluster under the same heading for `memory_list(domain='daily-log')`.

## Adding your own

Follow the same pattern:

```python
from langchain_core.tools import tool

@tool
async def my_tool(required_arg: str, optional_arg: int = 5) -> str:
    """First line becomes the LLM's summary of the tool.

    Args:
        required_arg: What this argument is. LLM reads these docstrings.
        optional_arg: Optional with a sensible default.
    """
    try:
        result = await do_the_thing(required_arg, optional_arg)
    except Exception as e:
        return f"Error: {e}"
    return f"Success: {result}"
```

Then append it to the keyless tool list in `get_all_tools()` — keep the conditional `_build_memory_tools(knowledge_store)` extension below it so the bundled memory tools still ship when a store is configured:

```python
def get_all_tools(knowledge_store=None):
    tools = [current_time, calculator, web_search, fetch_url, my_tool]
    if knowledge_store is not None:
        tools.extend(_build_memory_tools(knowledge_store))
    return tools
```

See [Write your first tool](/tutorials/first-tool) for the full walkthrough.

## Related

- [Configure subagents](/guides/subagents) — tools are allowlisted per subagent
- [Environment variables](/reference/environment-variables) — SSRF allowlist vars affect `fetch_url`
- [Eval your fork](/guides/evals) — the eval harness exercises every tool listed here end-to-end
