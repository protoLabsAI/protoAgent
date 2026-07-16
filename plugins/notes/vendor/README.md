# Vendored CodeMirror 6

These are third-party bytes committed into the repo so the notes editor renders markdown
**offline** — the manifest declares `network: []`, which was a lie while the old preview
pulled `marked` off cdnjs. The plugin serves them itself from `/plugins/notes/vendor/<name>`
(allowlisted in `_VENDOR_FILES`), so the plugin stays git-installable with no host build
step (ADR 0038).

All packages are **MIT** licensed, © Marijn Haverbeke and contributors.
Upstream: <https://github.com/codemirror> and <https://github.com/lezer-parser>.

## What's here

| File | Package | Version |
| --- | --- | --- |
| `state.mjs` | `@codemirror/state` | 6.7.1 |
| `view.mjs` | `@codemirror/view` | 6.43.6 |
| `language.mjs` | `@codemirror/language` | 6.12.4 |
| `commands.mjs` | `@codemirror/commands` | 6.10.4 |
| `lang-markdown.mjs` | `@codemirror/lang-markdown` | 6.5.1 |
| `autocomplete.mjs` | `@codemirror/autocomplete` | 6.20.3 |
| `common.mjs` | `@lezer/common` | 1.5.2 |
| `highlight.mjs` | `@lezer/highlight` | 1.2.3 |
| `lr.mjs` | `@lezer/lr` | 1.4.10 |
| `markdown.mjs` | `@lezer/markdown` | 1.7.2 |
| `process.shim.mjs` | — | hand-written, see below |

## Two traps, if you ever refresh these

**1. One copy of each package, or nothing works.** CodeMirror compares Facets, StateFields
and `EditorState` by *reference*. Two copies of `@codemirror/state` means `markdown()`'s
extensions are rejected by the other copy's `EditorView` with "Unrecognized extension
value". esm.sh's default `?bundle` inlines a private copy of `state` into **every**
package and fails exactly that way. Each file here is therefore built with `?external=`
so it emits **bare specifiers**, and the page's import map resolves each package to one
shared file. `tests/test_notes_plugin.py::test_import_map_covers_the_whole_vendored_closure`
enforces both halves (every specifier mapped; no package mapped twice).

**2. esm.sh injects a Node polyfill.** The `@lezer/lr` bundle opens with
`import __Process$ from "/node/process.mjs"` (it reads `process.env.LOG` to gate parser
debug logging). That path exists only on esm.sh's CDN, so a vendored copy 404s — and
because a failed module import aborts the whole graph, **CodeMirror never mounts and the
editor renders as an empty box with nothing thrown**. `process.shim.mjs` stands in for it
and the import map points `/node/process.mjs` at it.

## Regenerating

Each file is the *real* bundle, one level below the shim esm.sh returns for a `?bundle`
request — fetch `https://esm.sh/<pkg>?bundle&external=<list>`, then follow the
`export * from "…"` path in the ~290-byte response. Note `?bundle&external=` must not
list the package itself (`@lezer/lr` returns a bare re-export with no bundle to fetch if
it does). esm.sh's `/build` API is deprecated, so that route is not an option.

After refreshing, re-run the vendor tests and drive the page in a browser — the static
tests cannot tell you whether CodeMirror actually mounted.
