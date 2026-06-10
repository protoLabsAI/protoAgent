# Write your first tool

You have a running agent from the [previous tutorial](/tutorials/first-agent). Now you'll add a custom tool, restart the container, and watch the lead agent call it.

The tool in this tutorial is deliberately silly — it reports the current git SHA of the repo on disk. The point is to learn the wire-up, not the domain logic.

## 1. Write the tool

Open `tools/lg_tools.py` and add, near the other `@tool` functions:

```python
import subprocess

@tool
async def git_sha(short: bool = True) -> str:
    """Return the current git SHA of the agent's source tree.

    Args:
        short: If True (default), return the 7-character short SHA.
            If False, return the full 40-character SHA.
    """
    args = ["git", "rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error: could not run git: {e}"
    if out.returncode != 0:
        return f"Error: git exited {out.returncode}: {out.stderr.strip()}"
    return out.stdout.strip()
```

Then register it in `get_all_tools()` at the bottom of the same file:

```python
def get_all_tools(knowledge_store=None, scheduler=None, inbox_store=None, beads_store=None):
    tools = [current_time, calculator, web_search, fetch_url, ask_human, request_user_input]
    tools.append(git_sha)   # ← new
    # ... the function then extends `tools` with github/notes/memory/scheduler/
    #     inbox/beads/peer tools and applies the `tools.disabled` denylist.
    return tools
```

(For a real fork the no-edit path is a [plugin](/guides/plugins) — `register_tools([git_sha])` — which adds the tool without touching `get_all_tools`. Editing the function directly, as here, is the quickest way to see it work in the tutorial.)

## 2. Allow the subagent to use it (optional)

If you want the researcher subagent to be able to call `git_sha`, add it to the allowlist in `graph/subagents/config.py`. Append rather than replace — dropping the bundled defaults removes the researcher's memory tools:

```python
RESEARCHER_CONFIG = SubagentConfig(
    # ...
    tools=[
        "current_time", "web_search", "fetch_url",
        "memory_recall", "memory_ingest", "memory_list",
        "git_sha",   # ← new
    ],
    # ...
)
```

(Subagent tool allowlists are strict — tools not listed here are invisible to the subagent even if they're registered with the lead agent.)

## 3. Rebuild and restart

```bash
docker build -t my-agent:local .
docker run --rm -p 7870:7870 \
    -e AGENT_NAME=my-agent \
    -e OPENAI_API_KEY="$LITELLM_MASTER_KEY" \
    my-agent:local
```

## 4. Ask the agent

In the chat UI:

> What SHA is the agent running right now?

You should see a tool-call card for `git_sha` (running → done, with the result), and then a response weaving the SHA into natural language. (In the React console it renders as a collapsible tool-call card.)

## What to notice

- The tool's **docstring is the LLM's spec**. First line is the summary; `Args:` entries become the parameter docs the LLM reads. Spend effort on docstrings.
- **Errors are strings, not exceptions**. `return f"Error: ..."` lets the LLM read the failure and retry or give up gracefully. If you `raise`, the exception bubbles to the A2A handler and surfaces as a 500 to the caller.
- The `@tool` decorator makes the function callable via `.ainvoke({"key": value})` in tests, and async LangGraph nodes in production. The template's test suite uses the former pattern.

## Testing your tool

Add a test next to `tests/test_starter_tools.py`:

```python
import pytest

@pytest.mark.asyncio
async def test_git_sha_returns_something_sha_shaped():
    from tools.lg_tools import git_sha
    result = await git_sha.ainvoke({})
    # Either a 7-char hex SHA or a clean error — never an exception
    assert result.startswith("Error:") or len(result) == 7
```

The template runs tests via `pytest` with `pytest-asyncio` in auto mode — no extra setup.

## Where to go next

- [Add a custom skill](/guides/add-a-skill) — advertise new capabilities on the agent card so A2A callers can find them
- [Starter tools reference](/reference/starter-tools) — the shapes of the tools that ship by default
- [Configure subagents](/guides/subagents) — add specialized delegates beyond the shipped `researcher`
