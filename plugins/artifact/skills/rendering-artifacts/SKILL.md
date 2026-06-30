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
- `markdown` — a Markdown document (notes, a README, a write-up). Rendered with design-system
  prose styling; GitHub-style tables/lists/code work, and a ` ```mermaid ` fence becomes a live
  diagram. Reach for this over `html` when you just want **formatted text**.
- `html` — a full or partial HTML document (with inline `<style>`/`<script>` as needed).
- `svg` — inline SVG markup (icons, simple charts).
- `react` — a self-contained component script that renders into `#root`; React, ReactDOM, and
  Babel are provided. Write the component **and** the `ReactDOM.createRoot(...).render(...)` call.
  React artifacts can also `import` from a curated **offline** set (see below).

## Richer React: charts, icons, and design-system components

`react` artifacts may `import` (ES modules) from this curated, fully-offline set — no network:

- **`@pl/ui`** — protoLabs design-system component wrappers that match the console theme:
  `Button` · `Card` · `Stat` · `Badge` · `Alert` · `Tag` · `Kbd` · `Input` · `Icon` (a
  [lucide](https://lucide.dev) icon by `name`, e.g. `<Icon name="rocket" />`).
- **`chart.js`** — `import { Chart } from 'chart.js'` (controllers pre-registered) for quick
  bar/line/pie/etc. charts onto a `<canvas>`.
- **`d3`** — `import * as d3 from 'd3'` for bespoke/data-driven SVG visualisations.
- **`lucide`** — the raw icon library, if you're not using `@pl/ui`'s `Icon`.
- **`react`** / **`react-dom/client`** — also importable (`import { createRoot } from
  'react-dom/client'`); they resolve to the same React the globals use.

```jsx
import { createRoot } from 'react-dom/client';
import { Card, Stat, Button, Icon } from '@pl/ui';
import { Chart } from 'chart.js';

function App() {
  const ref = React.useRef(null);
  React.useEffect(() => {
    new Chart(ref.current, { type: 'bar',
      data: { labels: ['A','B','C'], datasets: [{ label: 'n', data: [3,7,5] }] } });
  }, []);
  return (
    <Card>
      <Stat value="7" label="peak" /> <Icon name="trending-up" />
      <canvas ref={ref} width={320} height={160} />
      <Button variant="primary">OK</Button>
    </Card>
  );
}
createRoot(document.getElementById('root')).render(<App />);
```

You can also style plain elements with the design system's `.pl-*` classes (e.g.
`className="pl-btn pl-btn--primary"`) and `--pl-*` CSS tokens in **any** `html`/`react`/`markdown`
artifact — they're injected so artifacts match the console's live theme. Only these libraries are
available; for anything else, write the code inline (the sandbox has no other network access).

## Editing an artifact (don't re-create it)

When the user asks to change something you already rendered, **iterate the same artifact** — don't
call `show_artifact` again (that makes a near-duplicate and clutters the panel). Use:

- **`update_artifact(old_string, new_string)`** — a targeted edit. `old_string` must appear in the
  current source **exactly once** (copy it verbatim, whitespace included; add surrounding context
  to make it unique). This is the fast path — prefer it for small changes. Creates a new version.
- **`rewrite_artifact(code, title?)`** — replace the whole source. Use for large changes where a
  targeted edit would be awkward. Creates a new version; the kind is kept.

Each edit is a **version** the user can step back through in the panel, so iterate freely — you're
never destroying the previous version. Both default to the most-recent artifact; pass
`artifact_id` to target another.

## Did it render? (closing the loop)

A React artifact that throws at render time (a bad import, an undefined component, or defining a
component but never calling `render()`) used to fail **silently** — you'd get "Created" and no hint
it broke. Now the sandbox reports its render result back:

- When the panel is open, `show_artifact` / `update_artifact` / `rewrite_artifact` wait briefly and
  **append the render verdict to their reply** — e.g. *"⚠ But it FAILED to render: Icon is not
  defined"*. When that happens, **fix it** with `update_artifact` / `rewrite_artifact`; the artifact
  still exists, so iterate on it (don't start over). A clean render says so too.
- **`check_artifact(artifact_id?)`** — ask for the latest render verdict yourself (rendered cleanly
  / failed with the error / no result yet). Useful when the create reply came back before the
  render finished, or the panel was closed (open it to render).

Treat a render error as the signal to iterate — that's the code→render→fix loop. Don't apologise and
guess; read the error and make the targeted edit.

## Managing artifacts

- **`list_artifacts()`** — see the ids/kinds/titles/version counts (to target an edit or delete).
- **`check_artifact(artifact_id?)`** — the latest render verdict (see above).
- **`delete_artifact(artifact_id)`** — remove one for cleanup. (The user can also delete from the
  panel's trash button.)

## Interactive artifacts (calling back to you)

`html` and `react` artifacts can call **`window.protoArtifact.ask(prompt)`** — it returns a
Promise resolving to *your* answer — so an artifact can be a live mini-app (a game NPC, a tutor,
a generator). Use it when the user asks for something that needs intelligence *inside* the widget:

```js
const line = await window.protoArtifact.ask("Greet the player as a grumpy dwarf, one line.");
```

It only works if the operator set `ARTIFACT_ASK_ENABLED` — if it's off, `ask()` rejects with a
message telling them how to enable it, so write artifacts that degrade gracefully.

## When to still write files

Only when the user explicitly wants a **project / files** ("scaffold a repo", "write the component
to a file", "create a Vite app"). For "show me a counter widget" → `show_artifact("react", …)`.
