# Knowledge browser by collection — design spec

Status: **APPROVED, ready to build** (Josh, 2026-11-08: "knowledge collection view looks great"). Owner: designSystem. Issue: #4630 slice 6. Server API: #4906.

> **Build notes from the preview render (2026-11-08):** use the real DS props. Pass `Callout tone=` (not `variant=`) and `Empty description=` (not `desc=`). The delete and stop-sharing confirm is `ConfirmDialog`, given `open`. Use the DS `Input`, `Tabs`, `Select`, `Table` and `Menu` components this spec names, not raw `pl-*` markup.

- Surface: core console, Knowledge surface in `apps/web` (`apps/web/src/knowledge/KnowledgeStore.tsx`), issue #4630 slice 6
- Follows: ADR 0125 (memory and knowledge split; knowledge rows are `chunks` grouped by `collection`)
- Requested by: Josh, 2026-11-08
- Status: draft for Josh's approval. Nothing gets carded until he approves this spec and the preview.
- Uses: `@protolabsai/ui` and `@protolabsai/design` only. No new design-system component is required (see §8).

## 1. What this view answers

The Knowledge browser answers three questions, in this order:

1. **What does this agent know, in which collections, and which of them does it actually read?**
2. **What is inside one collection?** Its documents, and the chunks in each document.
3. **Where does this phrase appear?** In one collection, or across all of them.

It also lets Josh delete a whole collection, with an undo.

The test case is today's protoEngineer audit. A stale 153-chunk import supplied 60% of the knowledge injected into its prompts. On this view, that collection must stand out in the first screen, from three facts side by side: its size, its age, and its share of what the agent reads. It is shown as information, not as a warning. A big, old, often-read collection can be exactly right; Josh decides.

### 1.1 What changes from today

- Today the browser lists chunks flat, sometimes grouped by `source`, and labels each with `domain`. `collection` appears nowhere in `apps/web/src`.
- After this slice, the browser's first level is the **collection list**. Chunks are one level down.
- Memory rows no longer appear here. They live in the Memory inspector (slice 6, its other half). The "pending review" filter and the review actions move with them, because agent-written rows are now memories (ADR 0125 D6). See Q9.
- The word "domain" leaves the UI. The add-entry form's domain field becomes a collection picker (§5.10).

## 2. Information architecture

The surface has two levels, inside the existing Knowledge panel.

1. **Collections** (the landing view). One row per collection, with a search box above.
2. **Collection detail**. Opened by selecting a row. It shows the collection's facts, its documents, and the chunks of each document. A back link returns to the list with the scroll position and sort kept.

Search is one box that works at both levels (§4).

The level is part of the URL or the UI store, so the browser's back button and a reload both return to the same collection (Q5).

### 2.1 Fields on a collection

| Order | Field | Source (proposed API, Q1) | Shown as |
|---|---|---|---|
| 1 | Name | `collection` | The collection's name as written, for example "claude-import". Largest text in the row. |
| 2 | Kind and source | `kind`, `source` | A short phrase: "Imported · claude-import.jsonl", "Folder · ~/notes/rules", "Plugin docs · atlas-plugin", "Web pages · 4 sites". The full path is in a `Tooltip`. |
| 3 | Size | `document_count`, `chunk_count` | "12 documents · 153 chunks" |
| 4 | Last updated | `updated_at` | "Updated 3 days ago", "Updated 4 months ago". The full date is in a `Tooltip`. |
| 5 | Share of what the agent reads | `retrieved_share_7d`, `retrieved_turns_7d` | A `Progress` bar and a number: "60% of retrieved knowledge". Under it, muted: "used in 41 of 58 turns, last 7 days". |
| 6 | Shared with the fleet | `tier` | A lowercase `Badge` "shared" when the collection lives in the commons tier. Nothing for private collections. |
| — | Internal ids, `domain`, raw `source_type` | — | **Never shown.** |

**Share of what the agent reads** is the field that answers the audit. It is the fraction of knowledge chunks injected into this agent's prompts over the last 7 days that came from this collection. The shares of all collections add up to 100%. "Used in N of M turns" says how often the collection showed up at all. Both are needed: a collection can be used in every turn but supply little, or be used rarely but dominate when it is.

If the backend can't provide retrieval numbers yet, this column shows "Not tracked yet" in muted text, and the default sort falls back to size (§3.2). The rest of the view still works. See Q2: this is the one backend addition the view needs to meet the brief.

## 3. The collection list

### 3.1 Layout at 1440 px

A DS `Table` inside the panel body, under the search box. Columns:

| Collection | Size | Last updated | Share of what the agent reads (7 days) | |
|---|---|---|---|---|
| **claude-import** · Imported · claude-import.jsonl | 12 documents · 153 chunks | 4 months ago | ▓▓▓▓▓▓░░░░ 60% · used in 41 of 58 turns | ⋯ |
| **release-runbooks** · Folder · docs/runbooks | 9 documents · 64 chunks | 2 days ago | ▓▓░░░░░░░░ 22% · used in 19 of 58 turns | ⋯ |
| **atlas-plugin docs** · Plugin docs · atlas-plugin `shared` | 31 documents · 210 chunks | 6 days ago | ▓░░░░░░░░░ 14% · used in 12 of 58 turns | ⋯ |
| **adr-index** · Folder · docs/adr | 128 documents · 402 chunks | yesterday | ░░░░░░░░░░ 4% · used in 5 of 58 turns | ⋯ |
| **old-ci-notes** · Imported · ci-notes.md | 1 document · 18 chunks | 7 months ago | ░░░░░░░░░░ 0% · not used in the last 7 days | ⋯ |

- The row's name cell is the link into the collection. The whole row is clickable for a pointer, but the keyboard target is the name (one tab stop per row, plus the row menu).
- The last column is a ghost icon `Button` (`aria-label="Actions for claude-import"`) that opens a `Menu`: **Open**, **Search in this collection**, **Delete collection…**.
- Columns are sortable by their header buttons (`aria-sort`). Default sort: **share of what the agent reads, highest first**.
- `Table caption`: "Knowledge collections for protoEngineer". `source`: "Retrieval figures cover the last 7 days."
- A summary line sits above the table, in the `PanelHeader` kicker: "5 collections · 847 chunks".

### 3.2 Spotting a collection like the audit's

No row gets warning colour. The three facts sit next to each other, so a large, old, heavily read row reads as such. On top of that, one quiet note helps:

- When one collection supplies **50% or more** of retrieved knowledge, a `Callout tone="info"` sits above the table:
  > **claude-import supplies 60% of the knowledge this agent reads.** It was last updated 4 months ago. If that's not what you expect, open it to check what's inside.
  >
  > [Open claude-import]

The threshold is a design default (Q3). The callout is not dismissible per session, because it disappears on its own when the share drops. It never appears when retrieval is not tracked.

### 3.3 Layout at 390 px

The table becomes a list. Each collection is one row, a composite `<button>` with multiple children (sanctioned for composite rows by protoContent#551), using the DS focus ring and tokens:

```
claude-import                                 shared?
Imported · 153 chunks · updated 4 months ago
▓▓▓▓▓▓░░░░  60% of what it reads
```

- The kind and source path drop to the detail view. Size shows chunks only.
- The row menu moves into the detail view's header, so each row has a single tap target at least 44 px tall.
- A sort `Select` ("Sort by: share read / size / last updated / name") replaces the column headers.
- The info callout from §3.2 stays above the list, full width.

## 4. Search

One `Input type="search"` with a leading search icon and a clear button (`onClear`), above the list or detail.

### 4.1 Scope

A segmented `Tabs` control sits next to the box with two options:

- **All collections**
- **{collection name}** — present only inside a collection, and selected by default there.

On the collection list, the scope is always "All collections", so the control is hidden and the placeholder reads "Search all knowledge…". Inside a collection, the placeholder reads "Search in claude-import…". "Search in this collection" from a row menu opens that collection with the box focused.

### 4.2 Results

- Results replace the list or the document tree while the box has text, and the clear button restores the previous view and scroll position.
- Each result is a chunk card: the matched text with the match highlighted (`<mark>`, styled with tokens), the document title, and, for an all-collections search, the collection name as a `TextLink` that opens that collection.
- Results are ordered by relevance. A count line leads them: "18 matches in 3 collections" or "6 matches in claude-import".
- An all-collections search groups nothing by default; relevance order matters more than grouping. Q6 asks whether a "by collection" grouping is wanted.
- The search runs when typing pauses (the existing debounce). It does not send `prefix`, matching today's surface.

### 4.3 Copy

| Case | Text |
|---|---|
| Placeholder, list | Search all knowledge… |
| Placeholder, detail | Search in {collection}… |
| Count | {N} matches in {M} collections · {N} matches in {collection} |
| None, in a collection | Nothing in {collection} matches "{query}". [Search all collections] |
| None, everywhere | Nothing in this agent's knowledge matches "{query}". |

## 5. Collection detail

### 5.1 Header

- Back link: "← All collections" (`TextLink`, keyboard and screen reader name "Back to all collections").
- `PanelHeader` title: the collection name. Kicker: "Imported · claude-import.jsonl".
- Actions: **Delete collection…** (`Button variant="danger" size="sm"`). For a shared or plugin collection, see §5.8 and §5.9.

### 5.2 Facts

A `Stats` row of four `Stat`s, the same facts as the list row, with more room:

| Stat | Example |
|---|---|
| Share of what the agent reads | 60% |
| Used in | 41 of 58 turns |
| Size | 12 documents · 153 chunks |
| Last updated | 4 months ago (8 Jul) |

At 390 px the stats wrap into a 2 × 2 grid.

### 5.3 Documents

Below the facts, the collection's documents as an `Accordion`. Each `AccordionItem` header shows:

- the document title (or the file name if there is no title);
- "{N} chunks";
- "used in {N} turns" when retrieval is tracked per document (Q2), otherwise nothing.

Documents are ordered by how often they were used, then by title. A collection with one document opens it expanded.

### 5.4 Chunks

Inside an open document, its chunks in position order (`chunk position` from ADR 0125). Each chunk reuses today's chunk card: heading, content clamped to 6 lines with **Show full text**, edit and delete actions for private rows. The `domain` badge is removed. The `finding_type` badge stays only if the field survives the split (Q9).

A document with more than 50 chunks shows the first 50 and a **Show the next 50** button. No infinite scroll.

### 5.5 Copy

| Element | Text |
|---|---|
| Stat label | Share of what the agent reads |
| Stat label | Used in |
| Stat label | Size |
| Stat label | Last updated |
| Not used | Not used in the last 7 days |
| Not tracked | Not tracked yet |
| Document meta | {N} chunks · used in {N} turns |
| More chunks | Show the next 50 |

### 5.6 Delete a collection

1. **Delete collection…** opens a `ConfirmDialog` with `destructive`.
   - Title: **Delete claude-import?**
   - Body: "This removes all 153 chunks in 12 documents from protoEngineer's knowledge. It stops reading them from its next turn. You can undo this for a short time afterwards."
   - When retrieval is tracked, a second paragraph: "This collection supplied 60% of the knowledge it read in the last 7 days."
   - Confirm: **Delete collection**. Cancel: **Keep it**.
2. Confirm shows `loading` on the button.
3. On success, the view returns to the list with the collection gone, and a toast appears for 10 seconds (today's undo window): "Deleted claude-import (153 chunks). [Undo]". Focus moves to the next row's name, or the search box if no rows are left.
4. **Undo** restores the collection, puts it back in the list, and toasts "Restored claude-import."
5. If the delete fails: an inline `Alert status="warning"` inside the dialog: "Couldn't delete claude-import. Nothing was removed. [Try again]". The dialog stays open.

The delete uses the existing soft delete with a grace window (today's `delete-by-source` and `restore-by-source`), extended to a collection (Q4). Shift+click to skip the confirm is **not** offered for a whole collection: the blast radius is too large.

### 5.7 Delete a single chunk

Unchanged from today (confirm, or Shift+click to skip), inside the collection.

### 5.8 Shared collections (commons tier)

- The row and the header show the "shared" badge, and the header kicker adds "Shared with every agent on this box."
- The delete button reads **Stop sharing…** instead. Its confirm says: "Every agent on this box stops reading claude-docs. Each agent's private copy, if it has one, stays." This mirrors today's per-chunk Unshare.
- Chunks are read-only here, as today.

### 5.9 Plugin collections

- A plugin's docs are rebuilt by the plugin. If deleting one would only last until the plugin reloads, the delete button is replaced by a muted line: "Managed by atlas-plugin. Turn the plugin's docs off in its settings to remove it." with a link to the plugin's settings. Q7 asks what the backend actually does.

### 5.10 Add to knowledge

The existing **Add a source** and **Add an entry** dialogs stay. Their `domain` field becomes a **Collection** `Combobox`: existing collection names as suggestions, free text creates a new collection. Inside a collection, it is prefilled with that collection.

## 6. States

Every state below is in the preview.

| State | List | Detail |
|---|---|---|
| Loading | Three `Skeleton` rows under the table header; the kicker shows no count. | `Skeleton` stats and two `Skeleton lines={3}` blocks. |
| Knowledge off | `Empty` title "Knowledge is off for this agent" and description "Turn it on to let the agent read collections you add." with a **Open settings** button (today's copy names a config key; this replaces it). | — |
| No collections | `Empty` title "No knowledge yet" and description "Add a folder, a file or a web page and it becomes a collection this agent can read." with **Add a source**. | — |
| One collection | The table with one row. The §3.2 callout does not show (one collection is always 100%, which says nothing). | — |
| Many | As §3. | As §5. |
| Collection is empty (all chunks deleted one by one) | Row shows "0 chunks". | `Empty` "This collection is empty." with **Delete collection…**. |
| Being ingested | Row shows a `Spinner` and "Adding 3 of 12 documents…" in place of size. Opening it shows what has landed. | Same line above the documents. Delete is disabled with tooltip "Wait until it finishes adding." |
| Search, no results | §4.3 copy. | §4.3 copy with the "Search all collections" button. |
| Failed load | `Alert status="warning"` "Couldn't load collections. The agent still has its knowledge; only this view failed to load." with **Retry**. Never shows "No knowledge yet" when it simply doesn't know. | Same, "Couldn't load claude-import." |
| Collection gone (deleted elsewhere, or a stale URL) | — | `Callout tone="info"` "This collection isn't here any more. It may have been deleted." with **Back to all collections**. |
| Delete, undo, delete failed | §5.6 | §5.6 |

### 6.1 Themes and sizes

- Light and dark themes use tokens only: no literal colours, spacing values or radii.
- Names and chunk text use `--pl-color-fg`. Source, meta and "used in" lines use `--pl-color-fg-muted`, which measures 7.24:1 (dark) and 7.73:1 (light) on `--pl-color-bg-raised`. Do not use `--pl-color-fg-subtle` for any text here: it fails at 3.42:1 on the light surface.
- The share `Progress` uses `status="neutral"` in every row, including the 60% one. Colour never ranks the collections; the number does.
- The search match highlight is styled with tokens and keeps 4.5:1 for its text in both themes. The build checks it with `theme_contrast`.
- 1440 px: table. 390 px: list rows (§3.3), stats in a 2 × 2 grid, full-width buttons in the delete dialog.

## 7. Accessibility

- **Structure.** The panel title is the page's `h2`. In the detail view, "Documents" is an `h3`. The table has a `caption`; the phone list is a `ul` with an accessible name "Knowledge collections".
- **Rows.** On desktop, the name cell is a link and the row menu is a labelled button; the row's pointer click does nothing a keyboard user can't do with the name. On the phone, each row is one `<button>` whose accessible name reads "claude-import, imported, 153 chunks, updated 4 months ago, 60% of what it reads".
- **Progress bars.** Each has an accessible name ("Share of what the agent reads") and value text ("60%"). The visible number repeats it, so the bar is never the only carrier.
- **Sorting.** Header buttons expose `aria-sort`. A sort change is announced politely: "Sorted by share read, highest first."
- **Search.** The result count sits in a polite `role="status"` region. The scope `Tabs` is labelled "Search in".
- **Delete.** `ConfirmDialog` traps focus and returns it. The undo toast stays for 10 seconds; the Undo button is reachable by keyboard while it shows (WCAG 2.2.1: the time limit is the backend's grace window, and Q4 asks whether it can be longer).
- **Keyboard.** Every control is a DS `Button`, `TextLink`, `Input`, `Tabs`, `Select`, `Menu` or `AccordionItem`, apart from the sanctioned composite phone row. Nothing is hover-only; tooltips repeat visible information.
- **Colour.** No state is shown by colour alone.

## 8. Components

### 8.1 Design-system components used (all exist today)

| Component | Import | Use |
|---|---|---|
| `PanelHeader` | `@protolabsai/ui/navigation` | List and detail headers |
| `Table`, `THead`, `TBody`, `Tr`, `Th`, `Td` | `@protolabsai/ui/data` | Collection list at 1440 px |
| `Progress` | `@protolabsai/ui/data` | Share of what the agent reads |
| `Stats`, `Stat` | `@protolabsai/ui/layout` | Collection facts |
| `Accordion`, `AccordionItem` | `@protolabsai/ui/navigation` | Documents |
| `Tabs` (`variant="segmented"`) | `@protolabsai/ui/navigation` | Search scope |
| `Input` | `@protolabsai/ui/forms` | Search box, with `icon` and `onClear` |
| `Select` | `@protolabsai/ui/forms` | Sort on the phone |
| `Combobox` | `@protolabsai/ui/forms` | Collection picker in the add dialogs |
| `Menu`, `MenuItem` | `@protolabsai/ui/menu` | Row actions |
| `Button` | `@protolabsai/ui/primitives` | All actions |
| `Badge` | `@protolabsai/ui/primitives` | "shared" |
| `Callout` | `@protolabsai/ui/primitives` | The 50% note, collection gone |
| `Empty` | `@protolabsai/ui/primitives` | Off, no collections, empty collection |
| `TextLink` | `@protolabsai/ui/primitives` | Back link, collection link in results |
| `Alert` | `@protolabsai/ui/data` | Failed load, failed delete |
| `Skeleton`, `Spinner` | `@protolabsai/ui/data` | Loading, ingesting |
| `ConfirmDialog` | `@protolabsai/ui/overlays` | Delete or stop sharing |
| `Tooltip`, `useToast` | `@protolabsai/ui/overlays` | Full paths and dates; delete and undo |

### 8.2 Console composites (in `apps/web`)

| Composite | Purpose |
|---|---|
| `CollectionList` | Table or phone list by breakpoint, sorting, the 50% callout. |
| `CollectionRow` | One row in either layout. |
| `CollectionDetail` | Header, stats, documents accordion, chunk paging, delete flow. |
| `KnowledgeSearch` | The box, the scope tabs and the results list. |
| `useCollections()` | List, detail, delete, undo and search queries, through `lib/queries`. |

The existing chunk card in `KnowledgeStore.tsx` is kept and reused inside `CollectionDetail`, without the `domain` badge. The `kb-group` source grouping is replaced by the documents accordion.

### 8.3 Possible design-system contribution (optional, not blocking)

- **Responsive table.** The table-to-list switch at 390 px is a recurring console need (inventory, jobs views). If the build writes a second one, that is the signal to propose a stacked-row mode for `Table`. Not needed for this slice.

## 9. Out of scope

- The Memory inspector half of slice 6, which gets its own spec.
- Renaming or merging collections.
- Moving chunks between collections.
- Re-ingesting or refreshing a collection from its source. A "Refresh from source" action would answer "it's 4 months old", and is a natural follow-up (Q8).
- Retrieval history beyond 7 days, and charts.

## 10. Open questions

### For the coder and Claude Code, against the codebase

1. **Collection API.** Is there, or will slices 1–5 add, a `GET /api/knowledge/collections` returning per collection: name, kind, source, tier, document count, chunk count, `updated_at`? And a per-collection documents endpoint with chunk counts? Today's routes are chunk-level only.
2. **Retrieval numbers.** Nothing in `src/` records how often a chunk is injected (no `hit_count`, `retrieved_count` or `last_retrieved`). The brief's "how often it's retrieved" needs the knowledge projection to count, per turn, which collections (and ideally documents) supplied injected chunks. Is that a small addition to slice 3 (split budgets), or a separate card? Until then the view shows "Not tracked yet".
3. **The 50% threshold.** Is one collection supplying half the injected knowledge the right point for the info note, or should it depend on the number of collections?
4. **Collection delete.** Can `delete-by-source` / `restore-by-source` be extended to `delete-by-collection` with the same grace window? How long is the window, and could the undo toast last longer than 10 seconds for a collection?
5. **Deep links.** Does the Knowledge surface keep sub-view state in `uiStore` or the URL? The detail view should survive a reload and the browser's back button.
6. **Grouped results.** Is grouping all-collections results by collection wanted, or is relevance order enough?
7. **Plugin collections.** Does deleting a plugin's docs collection stick, or does the plugin rebuild it on reload? This decides §5.9.
8. **Refresh from source.** Do ingested folders and URLs keep enough provenance to re-ingest a collection in place?
9. **What leaves with memory.** Confirm that `review_state`, `ReviewChip`, `ReviewActions` and the pending-review filter move to the Memory inspector, and whether `finding_type` survives on `chunks`.

### For Josh

10. **Default sort.** Is "share of what the agent reads, highest first" the right default, rather than size or name?
11. **No warning colour.** Is it right that the audit's collection is surfaced by an info note and the numbers, never by warning colour?
12. **Delete safety.** Is a confirm dialog plus a short undo enough for deleting a whole collection, or do you want to type the name for large ones?
