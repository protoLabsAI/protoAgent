# Build your first plugin

Build a plugin with a tool, console view, and host-free tests. You need a running
agent from [Set up your first agent](/tutorials/first-agent). Use `protoagent` from
an installed package, or replace it with `uv run python -m server` in a checkout.
For a smaller tool-only example, see [Write your first tool](/tutorials/first-tool).

## 1. Scaffold it

```bash
protoagent plugin new "Word Count" --view --tests
```

The command prints the new directory, normally `<instance_root>/plugins/word-count/`, with a manifest, a `register()` entry point, a working tool, a
console view, and a host-free test suite:

```
<instance_root>/plugins/word-count/
├── protoagent.plugin.yaml   # the manifest — what the host reads before importing anything
├── __init__.py              # register(registry) — your contributions
├── tests/                   # a suite that runs with no protoAgent installed
│   ├── conftest.py          #   `plugin` + `registry` fixtures
│   ├── _plugin_testkit.py   #   the harness, vendored so the repo stands alone
│   └── test_word_count.py
├── .github/workflows/ci.yml
├── requirements-dev.txt
└── pyproject.toml
```

The scaffold starts disabled. Review its code before enabling it: plugins run
with the server's privileges.

## 2. Look at the manifest

```yaml
id: word-count
name: Word Count
version: 0.1.0
description: >-
  A protoAgent plugin.
enabled: false
config_section: word_count
views:
  - { id: main, label: "Word Count", icon: Boxes, path: /plugins/word-count/view }
```

`id` is the slug that namespaces everything your plugin owns — its routes, its config section, its
event topics. The whole file is parsed *before* your Python is imported, which is how the console
can list and configure a plugin it has never run. Every field is in the
[manifest reference](/reference/plugin-manifest).

## 3. Write the tool

Open `__init__.py`. Inside `register()`, replace the `word_count_hello` tool
definition and its `registry.register_tool(...)` call. Keep the scaffolded view
and data-router registrations below them. The function should begin with:

```python
from langchain_core.tools import tool


def register(registry):
    """Wire this plugin's contributions into the agent."""

    @tool
    def word_count(text: str) -> str:
        """Count words and characters in a passage of text.

        Args:
            text: The passage to measure.
        """
        words = len(text.split())
        return f"{words} words, {len(text)} characters"

    registry.register_tool(word_count)
    # Keep the scaffolded view and data-router registrations here.
```

Registration and tool selection follow two rules:

- **`register(registry)` runs at load and config reload.** It contributes tools before
  the graph is built; registration must tolerate being called again.
- **The docstring is the tool's interface.** The model reads it to decide when to call the tool and
  what to pass — it is prompt text, not a comment.

## 4. Enable it

In the console: **Settings ▸ Plugins ▸ Installed**, toggle *Word Count*. Or in
the instance's live `langgraph-config.yaml` (locate it with `protoagent config explain`):

```yaml
plugins:
  enabled: [word-count]
```

Now ask the agent in chat:

> How many words are in "the quick brown fox jumps over the lazy dog"?

It should call `word_count` and answer **9 words, 44 characters**. If it doesn't, check the server
log — a plugin that fails to load says so there, and a `register_*` call with bad arguments logs a
warning rather than raising.

## 5. Run the tests

```bash
cd <plugin-directory>
python -m pytest tests/ -q
```

These run with **no protoAgent host at all** — the [testkit](/reference/plugin-testkit) loads your
plugin the way the runtime does and stubs what's missing. That's what lets a plugin live in its own
repo with its own CI. Add a case for the tool you just wrote:

```python
def test_word_count_counts_both(plugin, registry):
    plugin.register(registry)
    tool = next(t for t in registry.tools if t.name == "word_count")
    assert tool.invoke({"text": "one two three"}) == "3 words, 13 characters"
```

The `plugin` and `registry` fixtures come from the scaffolded `conftest.py`: `plugin` is your
package with `register()` available, and `registry` is a fake that records what you contributed.

## 6. Open the view

Click the new icon in the left rail. The scaffolded page fetches from your plugin's own API route
and renders in the operator's current theme.

That page is a **sandboxed iframe**, which explains its shape: the page itself is served from a
*public* route (an iframe page-load can't carry a bearer token), while its data sits behind a
*gated* one. The `plugin-kit.js` handshake delivers the token and theme by `postMessage` — the
protocol is in the [view bridge reference](/reference/plugin-view-bridge).

## What you built

A directory that adds a tool and a console surface to a running agent, with its own tests, and no
core edits. Everything else is more of the same seam: routes, background surfaces, subagents,
middleware, scheduled work, event subscriptions.

**Next:**

- [Plugins guide](/guides/plugins) — the full contract, seam by seam
- [Plugin registry API](/reference/plugin-registry-api) — every `register_*` call
- [Plugin SDK](/reference/plugin-sdk-api) — run subagents, search knowledge, schedule work
- [Building a plugin view](/guides/building-react-plugin-views) — real UI, React and all
- [Install & publish plugins](/guides/plugin-registry) — ship it from a git URL
