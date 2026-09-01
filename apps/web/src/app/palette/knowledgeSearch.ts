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
//   • THE ROW CAP. That route does NOT clamp `k` (contrast `chat_routes.py`'s
//     `max(1, min(int(limit), 200))`), so the caller is the only clamp there is. We send an
//     explicit small `k` AND slice the response, so a backend that ignores the parameter
//     still can't paste 500 rows into the palette.
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
// Ids are namespaced `knowledge:<chunk id>`. The DS dedups FIRST-WINS with statics ahead
// of provider rows, so a bare numeric id could be swallowed whole by an unrelated static
// command that happened to claim it.
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

/** Rows we ask for, and the hard client-side cap. The palette root is a shortlist, not a
 *  browser — the Knowledge surface is where you page through the store. */
export const KNOWLEDGE_RESULT_CAP = 6;

/** How long one search may take before the row becomes "unavailable · timed out". */
export const KNOWLEDGE_TIMEOUT_MS = 4000;

/** Longest row label before ellipsis — a knowledge chunk's content is a paragraph. */
const LABEL_MAX = 72;

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function clip(text: string): string {
  return text.length > LABEL_MAX ? `${text.slice(0, LABEL_MAX - 1)}…` : text;
}

/** What the operator reads on the row: the chunk's heading when it has one, else the first
 *  line of its preview/content. Never empty — an untitled chunk still has to be pickable. */
export function knowledgeRowLabel(chunk: KnowledgeChunk): string {
  const heading = oneLine(chunk.heading ?? "");
  const body = oneLine(chunk.preview || chunk.content || "");
  return clip(heading || body || `Chunk ${chunk.id}`);
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
  const domain = oneLine(chunk.domain ?? "");
  return {
    id: `knowledge:${chunk.id}`,
    label: knowledgeRowLabel(chunk),
    group: KNOWLEDGE_GROUP,
    icon: createElement(Library, { size: 14 }),
    hint: domain || "knowledge",
    keywords: [
      "knowledge",
      "memory",
      "store",
      "note",
      "finding",
      "recall",
      domain,
      oneLine(chunk.heading ?? ""),
      oneLine(chunk.source ?? ""),
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
    keywords: ["knowledge", "memory", "error", "unavailable"],
    disabled: true,
    run: () => {},
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
      k: KNOWLEDGE_RESULT_CAP,
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
      return (rows ?? []).slice(0, KNOWLEDGE_RESULT_CAP).map((c) => toCommand(c, q, navigate));
    },
  };
}
