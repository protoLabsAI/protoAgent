// Live knowledge search in ⌘K — the console's FIRST DS `CommandProvider` (ADR 0057 ×
// ADR 0020). Everything else in the palette is a fixed list of PLACES; this is the first
// row that answers "where is the thing I'm looking for?" from the instance's own data.
//
// The DS's provider path (`command-palette.views.tsx`) already owns the hard parts, and
// this module deliberately owns NONE of them:
//
//   • DEBOUNCE. The commands view waits 120ms after the last keystroke before calling
//     `getCommands`. A second debounce here would stack onto that one — a quarter second
//     of dead air per keystroke, for no gain.
//   • CANCELLATION. It hands us an `AbortSignal` and aborts it when the query changes or
//     the palette closes. We THREAD that signal into `fetch` (`api.knowledgeSearch` now
//     forwards it) rather than opening a controller of our own: a private controller the
//     view can't reach would leave every superseded keystroke's request running to
//     completion against the FTS index.
//
// What is genuinely ours, because the DS cannot know it:
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
//   • THE DEADLINE. A hybrid store embeds the query over HTTP before it can search. Without
//     a ceiling, one slow embedding call leaves the palette showing "Searching…" until the
//     operator gives up. We derive a child signal (upstream abort OR deadline) so the
//     timeout cancels the request rather than merely ignoring it.
//   • THE FAILURE ROW. `Promise.allSettled` in the DS loop turns a REJECTED provider into
//     zero rows, which is pixel-identical to "nothing matched". So we never reject: a
//     failure resolves to one disabled row that says the search is unavailable and why.
//     An ABORT is not a failure — it is the operator typing another character — so an
//     aborted read resolves empty and silently.
//
// Every id is namespaced `knowledge:*` (`knowledge:<chunk id>`, `:more`, `:unavailable`).
// The DS dedups FIRST-WINS with statics ahead of provider rows, so a bare numeric id could
// be swallowed whole by an unrelated static command that happened to claim it.
import { createElement } from "react";
import { Library } from "lucide-react";

import type { Command, CommandProvider } from "@protolabsai/ui/command-palette";

import { api } from "../../lib/api";
import { errMsg } from "../../lib/format";
import type { KnowledgeChunk } from "../../lib/types";
import type { NavIntent } from "../usePaletteRegistry";

/** Provider id — also the handle a test (or a diagnostics view) finds it by. */
export const KNOWLEDGE_PROVIDER_ID = "knowledge-search";

/** Group heading the rows render under. */
export const KNOWLEDGE_GROUP = "Knowledge";

/** Shortest query we will search on. One character matches most of an FTS index and the
 *  empty string means "list recent chunks" server-side, so anything below this is noise. */
export const KNOWLEDGE_MIN_QUERY = 2;

/** Chunk rows we render, and the hard client-side cap. The palette root is a shortlist, not
 *  a browser — the Knowledge surface is where you page through the store, and `knowledgeMoreRow`
 *  is how you get there. (The request asks for `CAP + 1`; the spare is the overflow probe.) */
export const KNOWLEDGE_RESULT_CAP = 6;

/** How long one search may take before the row becomes "unavailable · timed out". */
export const KNOWLEDGE_TIMEOUT_MS = 4000;

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
  query: string,
  navigate: (intent: NavIntent) => void,
): Command {
  return {
    id: `knowledge:${chunk.id}`,
    label: knowledgeRowLabel(chunk),
    group: KNOWLEDGE_GROUP,
    icon: createElement(Library, { size: 14 }),
    hint: knowledgeRowHint(chunk),
    // Thin, and only FACTS ABOUT THIS ROW, because a remote-search row's keywords do not
    // decide whether it is shown: the server already applied the query and the palette
    // appends a provider's rows verbatim (`command-palette.views.tsx`, and #3289's ranked
    // root keeps that contract). All they can do is TIER the row when a ranked root orders
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
 *  DS drops a rejected provider's results on the floor, so a knowledge store that is down,
 *  slow, or 401ing would look exactly like a store with no match for the query. Listed but
 *  `disabled`, with the reason in `hint` — the seam's own convention for a row that should
 *  stay discoverable and explain itself. */
export function knowledgeErrorRow(reason: string): Command {
  return {
    id: "knowledge:unavailable",
    label: "Knowledge search unavailable",
    group: KNOWLEDGE_GROUP,
    icon: createElement(Library, { size: 14 }),
    // Clipped: `reason` is a server `detail` or an exception message, and an unbounded one
    // would push the row's own label off the line it is trying to explain.
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
 *  this row findable; all they could do is tier it under a ranked root (#3289), where a
 *  keyword matching the operator's query would lift the footer above the chunks it is a
 *  footer for. With nothing to match it ties with them and the stable sort leaves it last. */
export function knowledgeMoreRow(query: string, navigate: (intent: NavIntent) => void): Command {
  return {
    id: "knowledge:more",
    label: "All matches in Knowledge",
    group: KNOWLEDGE_GROUP,
    icon: createElement(Library, { size: 14 }),
    hint: `more than ${KNOWLEDGE_RESULT_CAP}`,
    run: (ctx) => {
      navigate({ kind: "knowledge", query });
      ctx.close();
    },
  };
}

/** Run one search under BOTH the palette's abort signal and our own deadline.
 *  Resolves `{ rows }` on success, `{ aborted: true }` when the palette superseded us, and
 *  `{ reason }` when it failed or ran out of time. Never rejects. */
async function search(
  query: string,
  signal: AbortSignal,
): Promise<{ rows?: KnowledgeChunk[]; aborted?: boolean; reason?: string }> {
  // A CHILD controller, not a competing one: it exists solely so the deadline can cancel
  // the in-flight request. It aborts when the palette's signal does (relayed) or when the
  // timer fires — so nothing survives the keystroke that superseded it.
  const ac = new AbortController();
  const relay = () => ac.abort();
  let deadlineHit = false;
  if (signal.aborted) ac.abort();
  else signal.addEventListener("abort", relay);
  const timer = setTimeout(() => {
    deadlineHit = true;
    ac.abort();
  }, KNOWLEDGE_TIMEOUT_MS);
  try {
    const data = await api.knowledgeSearch(query, {
      // One MORE than we render: the extra row never reaches the palette, it is purely the
      // probe that tells `getCommands` whether the shortlist is hiding anything.
      k: KNOWLEDGE_RESULT_CAP + 1,
      signal: ac.signal,
    });
    // `enabled: false` is an instance with no knowledge store at all — nothing to search
    // and nothing broken. Stay quiet rather than parking an error row under every query.
    if (data.enabled === false) return { rows: [] };
    return { rows: data.results ?? [] };
  } catch (err) {
    // The palette moved on (another keystroke, or the palette closed). It discards this
    // result either way; resolving empty keeps the abort out of the failure path.
    if (signal.aborted) return { aborted: true };
    return { reason: deadlineHit ? "timed out" : errMsg(err) };
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
    getCommands: async (query, { signal }) => {
      const q = (query ?? "").trim();
      if (q.length < KNOWLEDGE_MIN_QUERY) return [];
      const { rows, aborted, reason } = await search(q, signal);
      if (aborted) return [];
      if (reason !== undefined) return [knowledgeErrorRow(reason)];
      const found = rows ?? [];
      const out = found.slice(0, KNOWLEDGE_RESULT_CAP).map((c) => toCommand(c, q, navigate));
      // The probe asked for `CAP + 1`. Getting it back is the ONLY signal available that
      // matches exist past the shortlist — the route returns no total — so it is what
      // decides whether the operator is offered a way to see them.
      if (found.length > KNOWLEDGE_RESULT_CAP) out.push(knowledgeMoreRow(q, navigate));
      return out;
    },
  };
}
