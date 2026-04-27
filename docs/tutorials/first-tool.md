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
def get_all_tools(knowledge_store=None):
    return [
        current_time,
        calculator,
        web_search,
        fetch_url,
        git_sha,   # ← new
    ]
```

## 2. Allow the subagent to use it (optional)

If you want the worker subagent to be able to call `git_sha`, add it to the allowlist in `graph/subagents/config.py`. Append rather than replace — dropping the bundled defaults removes the worker's memory tools:

```python
WORKER_CONFIG = SubagentConfig(
    # ...
    tools=[
        "current_time", "calculator", "web_search", "fetch_url",
        "memory_ingest", "memory_recall", "memory_list", "memory_stats",
        "daily_log",
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

In the Gradio UI:

> What SHA is the agent running right now?

You should see a tool-start frame (`🔧 git_sha: ...`), a tool-end frame (`✅ git_sha → a1b2c3d`), and then a response weaving the SHA into natural language.

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
- [Starter tools reference](/reference/starter-tools) — the shapes of the five tools that ship
- [Configure subagents](/guides/subagents) — add specialized delegates beyond the placeholder `worker`
