# Build your first plugin

In the [previous tutorial](/tutorials/first-tool) you added a tool by editing the agent's own
source. That works, but it means your change lives in a fork you have to keep merging.

A **plugin** is the alternative: a self-contained directory that adds tools, routes, views, and
more, without touching core. In this tutorial you'll scaffold one, give it a tool the agent can
call, add a console view, and enable it — about twenty minutes, no fork.

You need a running agent from [Spin up your first agent](/tutorials/first-agent).

## 1. Scaffold it

```bash
python -m server plugin new "Word Count" --view --tests
```

That writes `plugins/word-count/` with a manifest, a `register()` entry point, a working tool, a
console view, and a host-free test suite:

```
plugins/word-count/
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

The plugin is **not enabled yet**, and it won't be until you say so. That's deliberate: a plugin
runs in-process with the agent's privileges, so enabling is always an explicit act.

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

Open `__init__.py`. The scaffold left you a `word_count_hello` tool — replace it with something
that does real work:

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
```

Two things worth noticing, because they're the whole plugin contract:

- **`register(registry)` is called once, at load.** It runs before the graph is built, so anything
  you register is in place by the agent's first turn.
- **The docstring is the tool's interface.** The model reads it to decide when to call the tool and
  what to pass — it is prompt text, not a comment.

## 4. Enable it

In the console: **Settings ▸ Plugins ▸ Installed**, toggle *Word Count*. Or in
`config/langgraph-config.yaml`:

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
cd plugins/word-count && python -m pytest tests/ -q
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
