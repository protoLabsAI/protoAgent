---
name: diagramming-code
description: When the user asks for a diagram OF THE CODE — a sequence diagram of a call flow, a flowchart or class diagram of a module's structure, a state diagram of a lifecycle — render it as a mermaid artifact whose nodes and messages link to the exact lines, grounded in code you actually read.
---

# Diagramming code (mermaid + code links)

A mermaid artifact can carry **code links**: the operator clicks a node or a message and the
console opens that exact range in the code pane (or their editor). That turns a diagram from a
picture into a map of the code. It is only worth it if every link is **true** — a diagram that
points at the wrong line is worse than no diagram.

## 1. Read first, draw second

- Find the real code before drawing anything: `search_files` for the entry point (it prints
  `file:line`), then `read_file` with an `offset` around each hit to follow the calls.
- Every linked node or message cites a `path` and `line` you **saw** in a `search_files` hit or a
  `read_file` offset. Never guess a line, never count lines by eye in a plain `read_file` result.
- **Every link carries an `anchor`**: a short, exact snippet of the line you mean, copied
  verbatim from the `search_files` / `read_file` output — `"runTool("`, `"llm.complete("`,
  `"function textFrom"` (≤ 120 chars, one line, distinctive enough to find the right spot).
  The server snaps the link to the anchor's occurrence nearest your `line`, so a miscounted
  line still lands on the right code; a link whose anchor isn't in the file is dropped. Anchor
  the line that DOES the thing (the call, the definition), not a comment about it.
- Draw what the code does, not what it probably does. If a step is inferred (a framework calls
  it, a callback is registered elsewhere), leave it unlinked or say so in the note.

## 2. Pick the diagram that fits the question

| The question | Diagram |
| --- | --- |
| "how does X handle Y", a request / call path, who calls whom in what order | `sequenceDiagram` |
| how a module or package is put together, a pipeline, branching logic | `flowchart` |
| types, their fields/methods and relationships | `classDiagram` |
| a lifecycle: statuses and what moves between them | `stateDiagram-v2` |

## 3. Keep it small

About **15 nodes or messages at most**. A bigger flow becomes an overview plus one diagram per
part ("here's the whole request; ask me to zoom into auth"). Short labels: a function name or a
few words — the link and its note carry the detail.

## 4. Attach the links

Pass `links` to `show_artifact` — a map from a **key** to a target:

```text
show_artifact(
  kind="mermaid",
  title="ask() with tool calls",
  code="""sequenceDiagram
  participant C as Client
  participant A as Agent
  participant T as Tools
  C->>A: ask(question)
  A->>T: run_tool(call)
  T-->>A: result
  A-->>C: answer""",
  links={
    "participant:Agent": {"project": "app", "path": "src/agent.py", "line": 12,
                          "anchor": "class Agent"},
    "msg:1": {"project": "app", "path": "src/agent.py", "line": 40, "end_line": 58,
              "anchor": "def ask(", "note": "ask() — builds the prompt and starts the tool loop"},
    "msg:2": {"project": "app", "path": "src/agent.py", "line": 61, "end_line": 70,
              "anchor": "run_tool(call)", "note": "one tool call per model turn, until it stops asking"},
    "msg:3": {"project": "app", "path": "src/tools.py", "line": 22, "anchor": "return result"},
  },
)
```

Keys:

- **flowchart / class / state**: the node id exactly as written (`A`, `Agent`, `Running`), or a
  flowchart `subgraph` id.
- **sequence participants**: `participant:<id>` or `participant:<alias>` (`participant:A` or
  `participant:Agent`).
- **sequence messages**: `msg:<n>` — the n-th message arrow in the source, counting from 1 (notes,
  `loop`/`alt` lines and participant lines don't count) — or `msg:<exact label>` when that label
  is unique.

Each target: `project`, `path` (relative to the project root), `line`, `anchor` (always — see
above), optional `end_line` (inclusive; the range moves with a snap) and optional `note` (one
sentence, ≤ 280 chars — the *why*, like `show_code`'s note).

## 5. Check the reply

The reply lists every link it attached **with the first line of each target echoed back** — read
them — and every link it **moved** to match its anchor ("moved 59→66 to match anchor
'runTool('"). If an echoed line still isn't the code you meant, your anchor was too generic:
fix it. It also lists links it **dropped** (outside the
project, a secret file, a line out of range, no such file) and keys that **match nothing** in the
diagram (a typo'd node id, `msg:9` in a 7-message diagram). Resend a corrected `links` map with
`update_artifact` / `rewrite_artifact` rather than leaving dead links. Then confirm it rendered
(`check_artifact`), as for any artifact.

## 6. Follow-ups revise the SAME artifact

"Zoom into auth", "add the retry path", "now show the error case" → iterate the artifact you
already made (the version arrows and the chat's version chips keep every step):

- `update_artifact(old_string, new_string)` for a small change — the links **carry over**. If the
  edit renumbers messages (you inserted one), pass the corrected `links` map with it.
- `rewrite_artifact(code, links=…)` for a new diagram in the same slot — links do **not** carry
  over a rewrite, so pass them.

## Don'ts

- Don't link to generated, vendored or minified files — link to the source that produces them.
- Don't paste the code into chat as well; the links are the pointer. Use `show_code` when one
  specific range deserves the operator's attention on its own.
- Links need the filesystem toolset and a registered project; without them, draw the diagram
  anyway and cite `path:line` in plain text.
