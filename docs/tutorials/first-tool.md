# Write your first tool

Add a tool that estimates reading time, then watch the agent call it. You need a
running agent from [Set up your first agent](/tutorials/first-agent). Use
`protoagent` from an installed package, or `uv run python -m server` in a checkout.

## 1. Scaffold a plugin

```bash
protoagent plugin new "Reading Time" --tests
```

The command prints the new plugin's directory, normally
`<instance_root>/plugins/reading-time/`. Open its `__init__.py` and replace the
scaffolded tool and registration with:

```python
import math
from langchain_core.tools import tool


def register(registry):
    @tool
    def reading_time(text: str, words_per_minute: int = 200) -> str:
        """Estimate how many minutes a passage takes to read.

        Args:
            text: The passage to measure.
            words_per_minute: Reading speed; must be positive.
        """
        if words_per_minute <= 0:
            return "Error: words_per_minute must be positive."
        words = len(text.split())
        minutes = math.ceil(words / words_per_minute)
        return f"{words} words; about {minutes} minutes to read."

    registry.register_tool(reading_time)
```

The docstring tells the model when to call the tool and what arguments it accepts.
The return value becomes the tool result the model reads.

## 2. Enable it

Open **Settings → Plugins → Installed** and enable **Reading Time**. Changes apply
on save. Confirm `reading_time` appears in **Settings → Tools**.

If it is missing, inspect the plugin's status and server log for an import error.
Fix the error and reload before retrying. The scaffold starts disabled because a
plugin runs with the server's privileges.

## 3. Ask the agent

Send:

> Use reading_time to estimate how long it takes to read "one two three four" at two words per minute.

You should see a `reading_time` tool-call card with **4 words; about 2 minutes to
read.** The agent then explains the result.

## 4. Test the tool

In the plugin's `tests/` directory, add:

```python
def test_reading_time_handles_invalid_speed(plugin, registry):
    plugin.register(registry)
    tool = next(t for t in registry.tools if t.name == "reading_time")
    result = tool.invoke({"text": "one two", "words_per_minute": 0})
    assert result == "Error: words_per_minute must be positive."


def test_reading_time_rounds_up(plugin, registry):
    plugin.register(registry)
    tool = next(t for t in registry.tools if t.name == "reading_time")
    result = tool.invoke({"text": "one two three", "words_per_minute": 2})
    assert result == "3 words; about 2 minutes to read."
```

From the plugin directory, run `python -m pytest tests/ -q` with pytest and the
plugin's dependencies installed. The scaffold's fixtures load the plugin and
record its contributions without a running server. See the
[testkit reference](/reference/plugin-testkit).

## Next steps

[Build your first plugin](/tutorials/first-plugin) adds a console view and a
publishable package. The [plugin guide](/guides/plugins) covers additional
contribution types; [configure subagents](/guides/subagents) to allow a worker
to use your new tool.
