// Live knowledge search in ⌘K — the console's FIRST DS `CommandProvider` (ADR 0057 ×
// ADR 0020). Everything else in the palette is a fixed list of PLACES; this is the first
// row that answers "where is the thing I'm looking for?" from the instance's own data.
//
// The ROOT VIEW owns the provider loop (`rootView.tsx`, #3289 — the host took the DS root
// over wholesale), and this module deliberately owns none of what lives there:
//
//   • DEBOUNCE. The root waits 120ms after the last keystroke before calling `getCommands`
//     (`PROVIDER_DEBOUNCE_MS`, the DS's own figure). A second debounce here would stack onto
//     that one — a quarter second of dead air per keystroke, for no gain.
//   • CANCELLATION. It hands us an `AbortSignal` and aborts it when the query changes or
//     the palette closes. We THREAD that signal into `fetch` (`api.knowledgeSearch` now
//     forwards it) rather than opening a controller of our own: a private controller the
//     view can't reach would leave every superseded keystroke's request running to
//     completion against the FTS index.
//   • CONTAINMENT and SELECTION. A provider that throws synchronously, resolves to a
//     non-array, or never settles cannot strand the spinner, and provider rows landing
//     asynchronously cannot move the highlight out from under an operator who has already
//     arrowed down — the root keys selection to the COMMAND ID, not the row index. Both
//     were `commandsView` defects; neither is worked around here, because there is nothing
//     left to work around.
//
// What is genuinely ours, because the root cannot know it:
//
//   • THE EMPTY-QUERY GUARD. `/api/knowledge/search` treats an empty `q` as "list the most
//     recent chunks" (operator_api/knowledge_routes.py) — a browse default the Knowledge
//     surface wants and the palette very much does not. Unguarded, merely OPENING ⌘K would
//     flood the root with knowledge rows. Below `KNOWLEDGE_MIN_QUERY` characters we do not
//     call the endpoint at all.
//   • THE ROW CAP, AND THE WAY PAST IT. That route does NOT clamp `k` (contrast
//     `chat_routes.py`'s `max(1, min(int(limit), 200))`), so the caller is the only clamp
//     there is. We ask for one row MORE than we show and slice the response, so a backend
//     that ignores the parameter still can't paste 500 rows into the palette — and that
//     spare row is also the only signal that the shortlist is hiding matches, which is what
//     the `All matches in Knowledge` footer row is for. Without it the cap is a dead end:
//     the operator's only route to the rest is to close ⌘K and retype the query by hand.
//   • THE DEADLINE, AND THE FACT THAT IT IS THE INNER ONE. A hybrid store embeds the query
//     over HTTP before it can search, so one slow embedding call would otherwise hold the
//     palette on "Searching…" until the operator gives up. The root has its own ceiling
//     (`PROVIDER_DEADLINE_MS`), but it can only RESOLVE the read to zero rows — it cannot
//     cancel a request a provider never wired to a signal, and zero rows reads as "nothing
//     matched". Ours does both jobs the root's cannot: it derives a child signal (upstream
//     abort OR deadline) so the timeout CANCELS the in-flight request, and it names the
//     failure on a row. It must therefore fire FIRST — `KNOWLEDGE_TIMEOUT_MS` is strictly
//     below the root's deadline, and `knowledgeSearch.test.ts` pins that ordering, because
//     equal values are a race whose two outcomes ("timed out" vs. silence) look nothing
//     alike to the operator.
//   • THE FAILURE ROW. `Promise.allSettled` in the root's loop turns a REJECTED provider
//     into zero rows, which is pixel-identical to "nothing matched". So we never reject: a
//     failure resolves to one disabled row that says the search is unavailable and why.
//     An ABORT is not a failure — it is the operator typing another character — so an
//     aborted read resolves empty and silently.
//
// Every id is namespaced `knowledge:*` (`knowledge:<i>:<tier>:<chunk id>`, `:more`,
// `:unavailable`). The root dedups FIRST-WINS on `Command.id` — statics ahead of provider
// rows — so the namespace is what stops an unrelated static command that happened to claim a bare
// numeric id from swallowing a result whole. The POSITION leads the id for the other half of
// the same problem: the dedup is blind to which provider a row came from, so two of OUR OWN
// rows sharing an id lose one of themselves the same silent way. A chunk id is a per-BACKEND
// rowid, not a global one — a layered store (ADR 0041) fuses a private and a commons DB that
// each autoincrement from 1 and de-dup on CONTENT, so one response routinely carries two
// different chunks under the same number, and a custom `KnowledgeBackend` may not set `id` at
// all (`operator_api/knowledge_routes.py` serializes `d.get("id")` — null and all). The index
// is unique by construction; `tier` rides along only to keep the id readable in a devtools
// dump. See `knowledgeSearch.test.ts` — "two tiers, one rowid".
import { createElement } from "react";
import { BookMarked } from "lucide-react";

import type { Command, CommandProvider } from "@protolabsai/ui/command-palette";

import { api } from "../../lib/api";
import { errMsg } from "../../lib/format";
import type { KnowledgeChunk } from "../../lib/types";
// From `./nav` DIRECTLY, not the `../usePaletteRegistry` barrel: `registry.ts` imports this
// module and the barrel re-exports `registry.ts`, so the barrel path is an import cycle.
import type { NavIntent } from "./nav";

/** Provider id — also the handle a test (or a diagnostics view) finds it by. */
export const KNOWLEDGE_PROVIDER_ID = "knowledge-search";

/** Group heading the rows render under — on the UNTYPED list, which is the only one the
 *  ranked root prints headers on (a relevance-ordered list has no sections). Still carried
 *  on every row regardless: `group` is part of the match haystack and part of what tiers a
 *  row, so it earns its place even where nothing renders it. */
export const KNOWLEDGE_GROUP = "Knowledge";

/** Attribution stamped on every row this provider returns, which the root renders as the
 *  row's SOURCE CHIP.
 *
 *  It is what tells the operator these rows came from the knowledge store rather than being
 *  more console commands, and the typed list has nothing else that says so: since #3289 the
 *  ranked root drops group headers on a typed query (they mark sections, and ranking sorts
 *  across groups), so a chunk row is a sentence of prose with a filename after it, sitting
 *  between **Settings** and a plugin view. The chip is the affordance the root offers in the
 *  header's place — `rootView.tsx` stamps `{ source, ...c }` per provider — and no core
 *  provider used it before this one. */
export const KNOWLEDGE_SOURCE = { id: "knowledge", label: KNOWLEDGE_GROUP };

/** Shortest query we will search on. One character matches most of an FTS index and the
 *  empty string means "list recent chunks" server-side, so anything below this is noise. */
export const KNOWLEDGE_MIN_QUERY = 2;

/** Chunk rows we render, and the hard client-side cap. The palette root is a shortlist, not
 *  a browser — the Knowledge surface is where you page through the store, and `knowledgeMoreRow`
 *  is how you get there. (The request asks for `CAP + 1`; the spare is the overflow probe.) */
export const KNOWLEDGE_RESULT_CAP = 6;

/** How long one search may take before the row becomes "unavailable · timed out".
 *
 *  STRICTLY BELOW the root view's own `PROVIDER_DEADLINE_MS`, and that is the whole point of
 *  the number rather than a coincidence of it. The root's ceiling resolves a hung provider to
 *  an empty array; ours cancels the request and returns a row that says what happened. At
 *  equal values the two race and the operator gets one of two completely different screens
 *  depending on timer order. `knowledgeSearch.test.ts` asserts the inequality, so a bump to
 *  either constant reds a test instead of silently re-opening the race. */
export const KNOWLEDGE_TIMEOUT_MS = 3000;

/** The console's mark for the Knowledge surface — the SAME icon `coreSurfaces.tsx` gives it
 *  on the rail and under `Open ▸`, so a knowledge row in ⌘K carries the mark of the place it
 *  lands you. Deliberately not `Library`, which is already spoken for one level down: the
 *  Knowledge surface uses it to flag a COMMONS chunk (`KnowledgeStore.tsx`), so wearing it
 *  here would put a "commons" badge on every result, most of which are not.
 *  One element, shared by all three row builders — a React element is an immutable descriptor,
 *  so reusing it across rows is not shared state. */
const KNOWLEDGE_ICON = createElement(BookMarked, { size: 14 });

/** Longest row label before ellipsis — a knowledge chunk's content is a paragraph. */
const LABEL_MAX = 72;

/** Longest trailing hint. Far shorter than a label: the hint shares the row with it, so an
 *  ingest filed under a long URL would otherwise squeeze out the thing it annotates. */
const HINT_MAX = 28;

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function clip(text: string, max = LABEL_MAX): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** What the operator reads on the row: the chunk's heading when it has one, else the first
 *  line of its preview/content. Never empty — an untitled chunk still has to be pickable. */
export function knowledgeRowLabel(chunk: KnowledgeChunk): string {
  const heading = oneLine(chunk.heading ?? "");
  const body = oneLine(chunk.preview || chunk.content || "");
  return clip(heading || body || `Chunk ${chunk.id}`);
}

/** The row's trailing text: where this chunk came from. The last segment of `source` (an
 *  ingest is filed under a path or a URL, and the tail is the part that names it), else the
 *  store's `domain` bucket, which the server always fills in ("general" when nothing else).
 *  NOT the word "knowledge" — the group heading sits directly above every one of these rows
 *  and already says it, so a hint repeating it spends the row's one metadata slot on nothing. */
function knowledgeRowHint(chunk: KnowledgeChunk): string | undefined {
  const tail = oneLine(chunk.source ?? "").split("/").filter(Boolean).pop() ?? "";
  return clip(tail || oneLine(chunk.domain ?? ""), HINT_MAX) || undefined;
}

/** One result → one DS command. `run` goes through the caller's navigator, never through
 *  `useUI.getState()`: the frameless desktop launcher mounts this same registry in a
 *  shell-less context where a direct store mutation is an inert no-op, and the launcher's
 *  navigator forwards the (serializable) intent to the real console window instead. */
function toCommand(
  chunk: KnowledgeChunk,
  index: number,
  query: string,
  navigate: (intent: NavIntent) => void,
): Command {
  return {
    // Keyed on the row's POSITION in this response, not on `chunk.id` alone — see the
    // module header. A chunk id is per-backend, so it is neither unique across a layered
    // store's tiers nor guaranteed present at all, and the root's first-wins dedup turns
    // either into a row that vanishes with no header, no count and no error.
    id: `knowledge:${index}:${chunk.tier ?? ""}:${chunk.id ?? ""}`,
    label: knowledgeRowLabel(chunk),
    group: KNOWLEDGE_GROUP,
    icon: KNOWLEDGE_ICON,
    hint: knowledgeRowHint(chunk),
    // Thin, and only FACTS ABOUT THIS ROW, because a remote-search row's keywords do not
    // decide whether it is shown: the server already applied the query and the root appends
    // a provider's rows verbatim (`orderCommands` sorts them, `rankCommands` never sees
    // them). All they can do is TIER the row when the ranked root orders
    // the results — which the row's own metadata can do and a generic word cannot: filler
    // like "memory" or "recall", identical on every chunk, sorts nothing and would only
    // make every knowledge row answer to a query about none of them.
    keywords: [
      oneLine(chunk.domain ?? ""),
      oneLine(chunk.source ?? ""),
      oneLine(chunk.source_type ?? ""),
      oneLine(chunk.finding_type ?? ""),
    ].filter(Boolean),
    run: (ctx) => {
      navigate({ kind: "knowledge", query });
      ctx.close();
    },
  };
}

/** The row a FAILED search resolves to. It exists because the alternative is silence: the
 *  root drops a rejected provider's results on the floor, so a knowledge store that is down,
 *  slow, or 401ing would look exactly like a store with no match for the query. Listed but
 *  `disabled`, with the reason in `hint` — the seam's own convention for a row that should
 *  stay discoverable and explain itself. */
export function knowledgeErrorRow(reason: string): Command {
  return {
    id: "knowledge:unavailable",
    label: "Knowledge search unavailable",
    group: KNOWLEDGE_GROUP,
    icon: KNOWLEDGE_ICON,
    // Clipped at the LABEL bound, not `HINT_MAX`: `reason` is a server `detail` or an
    // exception message, so an unbounded one would push the row's own label off the line it
    // is trying to explain — but this row's reason IS its content (it is the only row a
    // failed search returns, and it competes with no sibling for the line), so it gets the
    // long bound rather than the 28 characters a chunk row's provenance hint has to fit in.
    hint: clip(oneLine(reason)) || "search failed",
    // No keywords, like the footer row: this is the ONLY row a failed search returns, so
    // there is nothing for them to order and provider rows are never re-filtered.
    disabled: true,
    run: () => {},
  };
}

/** The footer row on a search with more matches than the shortlist shows — the only way
 *  out of the cap. `KNOWLEDGE_RESULT_CAP` exists because the palette root is a shortlist,
 *  but a cap with no overflow affordance is a dead end: the operator sees six rows, has no
 *  way to know a seventh existed, and reaches the rest only by closing ⌘K, opening the
 *  Knowledge surface and typing the query a second time. This row carries the same intent
 *  a chunk row does, so it lands them on the surface with the search already run.
 *
 *  Shown only when the probe came back FULL (see `getCommands`) — a standing "see all" row
 *  under every query would be a row of noise on every search that already showed everything.
 *
 *  Deliberately keyword-less. Provider rows are never re-filtered, so keywords cannot make
 *  this row findable; all they could do is TIER it under the ranked root, where a keyword
 *  matching the operator's query would lift the footer above the chunks it is a footer for.
 *  With nothing to match it lands in the residual tier and `orderCommands`' stable tiebreak
 *  (the row's index in the provider read) leaves it exactly where it was appended: last. */
export function knowledgeMoreRow(query: string, navigate: (intent: NavIntent) => void): Command {
  return {
    id: "knowledge:more",
    label: "All matches in Knowledge",
    group: KNOWLEDGE_GROUP,
    icon: KNOWLEDGE_ICON,
    // Says what the operator is looking at, not how many exist: "more than 6" leaves it
    // ambiguous whether 6 is the shown count or the total, and the row's mere PRESENCE is
    // already the "there are more" signal.
    hint: `showing top ${KNOWLEDGE_RESULT_CAP}`,
    run: (ctx) => {
      navigate({ kind: "knowledge", query });
      ctx.close();
    },
  };
}

/** How one search attempt ended. A CLOSED union rather than a bag of optionals: exactly one
 *  of the three is true, so the caller has to say which it handled and there is no fourth
 *  state ("rows and a reason", "neither") for a later edit to leak through. */
type SearchOutcome =
  | { kind: "rows"; rows: KnowledgeChunk[] }
  | { kind: "aborted" }
  | { kind: "failed"; reason: string };

/** Run one search under BOTH the palette's abort signal and our own deadline. Never rejects
 *  — `getCommands` turns each outcome into rows, and rejecting would be swallowed (see the
 *  module header). */
async function search(query: string, signal: AbortSignal): Promise<SearchOutcome> {
  // Already superseded before we started (the palette aborts on the next keystroke, and a
  // provider read can be scheduled behind one). Nothing downstream would use the answer, so
  // don't spend an FTS query on it.
  if (signal.aborted) return { kind: "aborted" };
  // A CHILD controller, not a competing one: it exists solely so the deadline can cancel
  // the in-flight request. It aborts when the palette's signal does (relayed) or when the
  // timer fires — so nothing survives the keystroke that superseded it.
  const ac = new AbortController();
  const relay = () => ac.abort();
  let deadlineHit = false;
  signal.addEventListener("abort", relay);
  const timer = setTimeout(() => {
    deadlineHit = true;
    ac.abort();
  }, KNOWLEDGE_TIMEOUT_MS);
  try {
    const data = await api.knowledgeSearch(query, {
      // One MORE than we render: the extra row never reaches the palette, it is purely the
      // probe that tells `getCommands` whether the shortlist is hiding anything.
      k: KNOWLEDGE_RESULT_CAP + 1,
      // What makes this a TYPE-AHEAD rather than a search box that happens to fire per
      // keystroke. The store's FTS5 index matches whole tokens (each one is quoted as a
      // phrase — `knowledge/store.py`), and semantic recall is off by default
      // (`embeddings: false`), so without this the default backend answers every partial
      // word with zero rows: "postg" finds nothing, "postgres" finds the chunk. Six empty
      // shortlists on the way to one full one, each indistinguishable from "no matches".
      // The flag widens only the LAST token, which is the one still being typed.
      prefix: true,
      signal: ac.signal,
    });
    // `enabled: false` is an instance with no knowledge store at all — nothing to search
    // and nothing broken. Stay quiet rather than parking an error row under every query.
    if (data.enabled === false) return { kind: "rows", rows: [] };
    return { kind: "rows", rows: data.results ?? [] };
  } catch (err) {
    // The palette moved on (another keystroke, or the palette closed). It discards this
    // result either way; resolving empty keeps the abort out of the failure path.
    if (signal.aborted) return { kind: "aborted" };
    return { kind: "failed", reason: deadlineHit ? "timed out" : errMsg(err) };
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", relay);
  }
}

/** Build the knowledge-search provider. `navigate` is INJECTED (rather than imported) so
 *  the launcher's forwarding sink and a test's spy are the same seam the console uses. */
export function knowledgeSearchProvider(
  navigate: (intent: NavIntent) => void,
): CommandProvider {
  return {
    id: KNOWLEDGE_PROVIDER_ID,
    source: KNOWLEDGE_SOURCE,
    getCommands: async (query, { signal }) => {
      const q = (query ?? "").trim();
      if (q.length < KNOWLEDGE_MIN_QUERY) return [];
      const outcome = await search(q, signal);
      if (outcome.kind === "aborted") return [];
      if (outcome.kind === "failed") return [knowledgeErrorRow(outcome.reason)];
      const found = outcome.rows;
      const out = found.slice(0, KNOWLEDGE_RESULT_CAP).map((c, i) => toCommand(c, i, q, navigate));
      // The probe asked for `CAP + 1`. Getting it back is the ONLY signal available that
      // matches exist past the shortlist — the route returns no total — so it is what
      // decides whether the operator is offered a way to see them.
      if (found.length > KNOWLEDGE_RESULT_CAP) out.push(knowledgeMoreRow(q, navigate));
      return out;
    },
  };
}
