---
name: rendering-artifacts
description: When the user wants to SEE, render, visualize, preview, or "show me" a chart, diagram, mock-up, table, or interactive widget — render it with the show_artifact tool instead of writing files to disk.
---

# Rendering artifacts (generative UI)

The console has an **Artifact panel** powered by the `show_artifact` tool. Use it whenever the
user wants to **look at something rendered** rather than get source files.

## When to use `show_artifact` (NOT the filesystem)

- "show me…", "render…", "visualize…", "draw…", "make a chart/diagram/flowchart of…",
  "build a little widget/demo to see…", "preview…"
- → call `show_artifact(kind, code)`; it renders sandboxed and the user sees it immediately.

Reach for `show_artifact` **before** writing files. Writing `.jsx`/`.html` to the workspace gives
the user files to wire up themselves — not what they asked for when they want to *see* it.

## Kinds

- `mermaid` — flowcharts, sequence/ER/gantt diagrams. `code` is the Mermaid definition.
- `html` — a full or partial HTML document (with inline `<style>`/`<script>` as needed).
- `svg` — inline SVG markup (icons, simple charts).
- `react` — a self-contained component script that renders into `#root`; React, ReactDOM, and
  Babel are provided. Write the component **and** the `ReactDOM.createRoot(...).render(...)` call.

## When to still write files

Only when the user explicitly wants a **project / files** ("scaffold a repo", "write the component
to a file", "create a Vite app"). For "show me a counter widget" → `show_artifact("react", …)`.
