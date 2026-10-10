---
name: rendering-artifacts
description: When the user wants to SEE, render, visualize, preview, or "show me" a chart, diagram, mock-up, table, or interactive widget — render it with the show_artifact tool instead of writing files to disk.
---

# Rendering artifacts (generative UI)

The console has an **Artifact panel** powered by the `show_artifact` tool. Use it whenever the
user wants to **look at something rendered** rather than get source files.

## The presentation ladder — prefer the lowest tier that answers

protoAgent can show an answer at four tiers. **Prefer the lowest tier that answers the
request**: each rung costs more than the one below it (a component more than text, a sandboxed
frame more than a component) and a plainer answer reads faster, so climb only when the rung
below genuinely can't carry the answer. The rule of thumb is **inline for an answer, panel for
a work product.**

| Tier | Mechanism | Runs code? | Reach for it when… |
|---|---|---|---|
| **Text** | markdown (prose, code, mermaid fences, KaTeX, GFM tables) | no | a fact, a short explanation, code, or a small static table — the answer is just *words and numbers* |
| **Data component** | `show_component` → `component-v1` | no | exact values, a record, ordered steps, a code pointer — structured data you want rendered natively, not a widget |
| **Inline artifact** | `show_artifact(…, placement="inline")` | yes (artifact sandbox) | an answer the user **interacts with right in the conversation** — a calculator, a what-if explainer, a chart, a small tool — that belongs beside your prose in scrollback |
| **Panel artifact** | `show_artifact(…)` (the default, `placement="panel"`) | yes (same sandbox) | a **work product** carried across turns — a document, a deck, a PDF, a large app — that the user opens, edits and returns to |

`placement` is a real argument on the tool — the full signature is
`show_artifact(kind, code, title="", links=None, placement="panel")`. It **defaults to
`"panel"`**, so a plain `show_artifact(kind, code)` still opens the panel; add `placement="inline"`
to climb to the inline tier. (`show_service`, the async variant, takes the same `placement`.)

An inline artifact **is** an artifact: same store, versions, render verdict,
`update_artifact`/`rewrite_artifact`, and an **Open in panel** button — only *where* it renders
differs. Inline lives in the transcript next to your message; panel opens the side panel.
`placement="inline"` is allowed for `html`, `svg`, `mermaid`, `react` and `vega-lite`; any other
kind renders in the panel. Default to **inline** for a self-contained answer and reserve the
**panel** for something the user will keep working on.

Inline placement is part of protoAgent's **interactive-answers** capability, and the **panel is
its universal fallback**: where a console or a `show_artifact` build doesn't offer inline
placement, the same call still renders the artifact in the panel — the answer is never lost. So
choose the **tier** by what the answer *is* (a calculator, a chart, a small tool), pass
`placement="inline"` to ask for it beside your prose, and describe the answer by **what it does**,
not by where it landed — then you're right whether it renders inline or in the panel.

## When to use `show_artifact` (NOT the filesystem)

- "show me…", "render…", "visualize…", "draw…", "make a chart/diagram/flowchart of…",
  "build a little widget/demo to see…", "preview…"
- → call `show_artifact(kind, code)`; it renders sandboxed and the user sees it immediately.

Reach for `show_artifact` **before** writing files. Writing `.jsx`/`.html` to the workspace gives
the user files to wire up themselves — not what they asked for when they want to *see* it.

## Kinds

- `mermaid` — flowcharts, sequence/ER/gantt diagrams. `code` is the Mermaid definition. The
  panel lets the user zoom and pan it. A diagram **of the code** can link its nodes and messages
  to the exact lines (`links=`) — see the `diagramming-code` skill.
- `markdown` — a Markdown document (notes, a README, a write-up). Rendered with design-system
  prose styling; GitHub-style tables/lists/code work, and a ` ```mermaid ` fence becomes a live
  diagram. Reach for this over `html` when you just want **formatted text**.
- `html` — a full or partial HTML document (with inline `<style>`/`<script>` as needed).
- `svg` — inline SVG markup (icons, simple charts).
- `vega-lite` — a **chart from data**: a JSON Vega-Lite spec with the rows inline in
  `data.values` (never `data.url` — it isn't loaded). The fastest way to chart: write the encoding,
  not a component. It's themed to the console automatically, so leave colours/background out, and
  it fills the panel width. Reach for this over `react` + chart.js whenever the chart is a
  standard mark (bar/line/area/point/arc/rect heat-map) over a table of rows. With the data
  plugin on, `data_chart(sql, spec, title)` runs the query and inlines the rows for you.
- `react` — a self-contained component script that renders into `#root`; React, ReactDOM, and
  Babel are provided. Easiest: **name your top-level component `App`** and it **auto-mounts** — you
  don't have to write the mount call. (You still can: `ReactDOM.createRoot(...).render(...)`; an
  explicit render always wins.) React artifacts can also `import` from a curated **offline** set
  (see below).

## Richer React: charts, icons, and design-system components

`react` artifacts may `import` (ES modules) from this curated, fully-offline set — no network:

- **`@pl/ui`** — protoLabs design-system component wrappers that match the console theme.
  Enough to prototype real **layouts** and **components**, not just widgets:
  - *Layout / structure* — `AppShell` (`{header, sidebar, aside}`) · `Header` (`{name, org, actions}`)
    · `SideNav` + `SideNavItem` (`{icon, active}`) · `Container` · `Section` · `Panel` · `Grid`
    (`{cols, gap, auto}`) · `Row` (`{label, desc, status}`) · `Hero` (`{cta}`) · `Divider`
  - *Navigation* — `Tabs` + `Tab` (`{active, icon}`) · `Segmented` + `SegmentedButton` ·
    `Menu` + `MenuItem` (`{icon, destructive}`) + `MenuSeparator`
  - *Data / content* — `Table` · `Board` · `Stats` + `Stat` (`{value, label}`) · `Steps` + `Step`
    (`{num, title}`) · `Progress` (`{value, max, variant}`) · `Accordion` + `AccordionItem`
    (`{title, open}`) · `Avatar` (`{src}`) · `Empty` (`{icon, title, desc, action}`)
  - *Overlays* — `Dialog` (`{title, onClose, footer}`) · `Drawer` (`{side, title, footer}`) ·
    `Callout` (`{variant, title}`) · `Tip`
  - *Forms* — `Field` (`{label, hint}`) · `Input` · `Textarea` · `Select` · `Switch` (`{label}`) ·
    `Checkbox` (`{label}`)
  - *Primitives / type* — `Button` (`{variant, size}`) · `IconButton` · `Card` · `Badge` · `Tag` ·
    `Kbd` · `Link` · `Dot` (`{variant, pulse}`) · `Spinner` · `Skeleton` · `Alert` · `Heading`
    (`{as}`) · `Eyebrow` · `Lead` · `Prose` · `Icon` (a [lucide](https://lucide.dev) icon by
    `name`, e.g. `<Icon name="rocket" />`)

  Prefer these over hand-rolled markup so a prototype matches the live theme; drop to a raw
  element with `className="pl-…"` for anything not wrapped here.
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

## Did it render? ALWAYS verify (closing the loop)

A React artifact can throw at render time (a bad import, an undefined component, an export
mismatch) even when the code looks right. **Verifying the render is a standard step, not an
optional one** — confirm it worked before you tell the user it's done:

- When the panel is open, `show_artifact` / `update_artifact` / `rewrite_artifact` wait briefly and
  **append the render verdict to their reply** — e.g. *"⚠ But it FAILED to render: Icon is not
  defined"* or *"It rendered cleanly."* Read it.
- If the reply didn't carry a clean verdict (it came back before the render finished, or said "no
  result yet"), **call `check_artifact` to confirm** — it waits briefly for the verdict. Make this
  your default after creating or editing an artifact.
- On a failure, **fix it** with `update_artifact` / `rewrite_artifact` — the artifact still exists,
  so iterate on it (don't start over), then verify again. Loop until it renders cleanly.

Treat a render error as the signal to iterate — that's the code→render→fix loop. Don't apologise and
guess; read the error and make the targeted edit.

## Managing artifacts

- **`list_artifacts()`** — see the ids/kinds/titles/version counts and which are pinned (to target
  an edit, pin or delete).
- **`check_artifact(artifact_id?)`** — the latest render verdict (see above).
- **`pin_artifact(artifact_id, pinned=True)`** — keep a **long-lived** artifact (a master resume, a
  reference doc, a plan you'll revisit) from being evicted. Only the most recent ~20 unpinned
  artifacts are kept, counting everything rendered on this agent, so an unpinned one silently
  disappears after enough unrelated renders — and any id you wrote down then points at nothing.
  Pin it as soon as you know it will outlive the conversation. Pins are capped: if it refuses, unpin
  one you no longer need (`pinned=False`). A cap of 0 means the operator turned new pins off —
  artifacts already pinned stay protected until unpinned. A pin keeps the latest versions, not
  every edit.
- **`delete_artifact(artifact_id)`** — remove one for cleanup. (The user can also delete from the
  panel's trash button.)

## Saving a generated FILE (docx / xlsx / pptx / pdf / image)

When a skill writes a real **file** to disk — a Word doc, a spreadsheet, a slide deck, a PDF, an
image — call **`save_file_artifact(path, title?, artifact_id?)`** right after, to put it in the Artifact panel as
a **versioned download artifact** with a Download button and a preview by type:

- `.docx` and `.pdf` → their **real pages** (scroll, zoom with −/Fit/+); `.pptx` → its **real
  slides** (current slide + filmstrip, arrow keys to page). Each keeps its extracted text underneath.
- `.csv`/`.tsv` → a table (any common delimiter; numbers right-aligned); `.xlsx` → one table per sheet.
- `.md` → rendered prose; `.json` → pretty-printed; images → a thumbnail; other text → plain text.

`path` can be relative to a filesystem project (as `find_files`/`read_file` report it — pass
`project` if more than one has it) or absolute. Re-saving the same document as a new revision? Pass the prior
`artifact_id` so it becomes v2, v3… of the same artifact instead of a new panel entry.

```text
# after cowork's docx skill writes /work/report.docx
save_file_artifact("/work/report.docx", "Q3 Report")
```

This is for files that already exist on disk. To *render* HTML/React/SVG/Markdown/charts, use
`show_artifact` (above) — don't write those to a file first.

**Showing a document that's already on disk** (a PDF a tool rendered, a deck, a report): ONE
`save_file_artifact` call on that file. Never `read_file` it and paste its contents into
`show_artifact` — that re-types the whole document through the model (a minute or more for a
few pages of HTML) and the copy drifts from the file.

## Interactive artifacts (calling back to you)

`html` and `react` artifacts can call **`window.protoArtifact.ask(prompt)`** — it returns a
Promise resolving to *your* answer — so an artifact can be a live mini-app (a game NPC, a tutor,
a generator). Use it when the user asks for something that needs intelligence *inside* the widget:

```js
const line = await window.protoArtifact.ask("Greet the player as a grumpy dwarf, one line.");
```

It only works if the operator set `ARTIFACT_ASK_ENABLED` — if it's off, `ask()` rejects with a
message telling them how to enable it, so write artifacts that degrade gracefully.

`html` and `react` artifacts can also use **`window.protoArtifact.send(text)`** — it puts `text`
into the chat **as a user message** and starts a normal, visible turn. Unlike `ask`, `send` is
**on by default** where the console provides it: it costs exactly what typing costs and the
operator sees exactly what was sent. Use it to let the user act on a result, subject to three
rules:

- **Feature-detect it first.** `send` is part of the interactive-answers bridge and isn't on
  every console, so call it **only when it's actually there** —
  `typeof window.protoArtifact?.send === "function"` — and keep a plain fallback (show the
  result as text the user can read or copy) for when it isn't. An artifact that calls `send`
  unconditionally throws where the bridge is absent.
- **Only from a labelled button** — never on load, a timer, or an input change; it needs a real
  user gesture.
- **Put the selected values into the text,** because the agent sees only that text — the
  artifact's control state is never synced to the agent.

```js
// a "Ask about this scenario" button, after the user picks values
btn.addEventListener("click", () => {
  const msg = `Split a $${bill} bill ${people} ways with a ${tip}% tip.`;
  if (typeof window.protoArtifact?.send === "function") window.protoArtifact.send(msg);
  else showResult(msg); // degrade: keep the answer visible when the bridge is off
});
```

## Authoring order for `html` (so it previews while you write it)

An inline `html` artifact streams into a live preview as you write it, so author it top to
bottom in this order:

1. **`<style>` first.** Put all CSS in a leading `<style>` block. The preview stays hidden until
   your first `<style>` closes, so leading with it means the answer is **never shown unstyled**.
2. **Readable markup next.** Write the body so it reads as a finished answer *before any script
   runs* — real labels, headings and default values live in the HTML, not injected by JS.
3. **Scripts last.** Put `<script>` at the end. The preview strips scripts and runs them only
   once the markup is whole, so nothing legible should depend on a script having executed.

The same order helps `react` and `svg`: static, styled structure first, behaviour last.

## Quality bar for interactive answers

An inline artifact is an *answer*, so hold it to the bar a good answer meets. Every item below
is non-negotiable for a calculator, explainer, chart or tool:

- **Every enabled control does real work.** No dead buttons, no inputs that change nothing. If a
  control isn't wired yet, disable it or leave it out.
- **Accessible controls.** Every input has a connected `<label>`; the widget is fully operable by
  **keyboard**; focus is **visibly** indicated (don't strip the focus ring without replacing it).
- **Validated numeric input.** Read with `valueAsNumber`, guard with `Number.isFinite`, clamp to
  sensible **domain bounds**, and refuse **zero divisors**. **Never** render `NaN`, `Infinity`, or
  a **stale** result left over from the last valid input — show a clear placeholder or an inline
  message instead.
- **Units and assumptions shown.** Label every quantity with its **unit**, and state on screen any
  **assumption** you baked in (tax rate, rounding, currency) — don't bury it in the math.
- **Reduced motion.** Respect `prefers-reduced-motion`, and give any animation a **pause** and a
  **reset** control.
- **Labelled provenance.** Mark each value as **sample**, **user-provided**, **retrieved**, or
  **calculated**, so the user can tell example data from their own input from a computed result.
- **`send` carries the values.** Call `protoArtifact.send(...)` only from a **labelled button**,
  **after feature-detecting it** (`typeof window.protoArtifact?.send === "function"`, with a plain
  fallback), with the **selected values** written into the text — the agent sees the text, never
  the controls.

## When to still write files

Only when the user explicitly wants a **project / files** ("scaffold a repo", "write the component
to a file", "create a Vite app"). For "show me a counter widget" → `show_artifact("react", …)`.
