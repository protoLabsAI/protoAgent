---
name: self-building-demo
description: >-
  Run the self-building demo (ADR 0096): live, in front of an audience, build a new
  plugin for whatever the user just asked for — design → scaffold → implement → test →
  hot-swap — and then USE it. Trigger when the operator says "demo the self-building
  loop", "show how you build plugins", "build yourself a tool for this (demo)", or asks
  to show protoAgent extending itself. Not for: routine plugin work (use the
  building-plugins skill directly).
---

# The self-building demo

You are the demo. The pitch is: *an app that builds itself around its user*. Narrate
each beat in one short line as you go — the audience should always know which step of
the loop they're watching. Total target: under five minutes to the payoff.

## The beats

1. **The ask.** Take the capability request (from the audience if offered). Check your
   toolset honestly — say you don't have a tool for it, and that you'll build one.
2. **Design — say it, don't subagent it.** One sentence: what the plugin is, its 1–2
   tools, where state lives. Only run `plugin-architect` for genuinely non-trivial
   designs; the demo's pace matters more than a spec.
3. **Scaffold.** `scaffold_plugin(name, with_tests=True)` — it enables live by itself.
   Use a FRESH name every run (a view/router plugin can't be cleanly re-installed
   without a restart; never reuse a prior demo's id).
4. **Implement.** Small plugin → edit it yourself with `plugin_write_file`. Substantial
   → `develop_plugin` and narrate that a coding agent is now working inside your plugin
   dir. THE CONTRACT: `register(registry)` must **call** `registry.register_tool(...)`
   with `@tool`-decorated functions — a returned list is silently ignored. Keep runtime
   state (data files) OUT of the plugin dir — write under the instance root.
5. **Test.** `test_plugin("<id>")` — read the verdict out loud. It runs in a disposable
   copy, so nothing live is at risk. Iterate on failures (the reload/test messages
   carry tracebacks and contribution counts — trust them over your assumptions).
6. **Hot-swap.** `reload_plugins` (or it's already live from the scaffold). Point at
   the console: the Plugins panel and any rail view update themselves — no restart.
7. **The payoff — NEXT turn.** New tools bind when the next turn starts, not mid-turn.
   End your build turn by saying exactly that ("ask me to use it"). On the next turn,
   USE the new tool on the user's real request. That moment is the demo.
8. **The kicker (optional, if asked "is this real?").** The plugin is a real repo shape
   (tests + CI from birth); `register_plugin_project` graduates it to a managed project
   a projectBoard can iterate on; bundles make it installable on any protoAgent.

## Traps that have actually burned this demo

- Calling the new tools in the SAME turn you built them — they aren't bound yet. Beat 7
  exists because of this; set the expectation instead of fumbling.
- A `register()` that returns tools instead of calling `registry.register_*` — loads
  "successfully" with zero tools. The reload message now flags it; read the message.
- Storing demo data in the plugin dir — an update wipes it, and before the test sandbox
  existed, a coder-written empty-state test deleted it live. Instance root, always.
- Reusing a plugin id from an earlier run — the scaffold refuses, or worse, a stale
  view lingers. Fresh id, every run.

If a step fails on stage: read the error to the audience and fix it with the loop's own
tools — recovering live IS the product demonstrating itself.
