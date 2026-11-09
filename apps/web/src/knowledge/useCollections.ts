import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { collectionChunksQuery, collectionsSearchQuery, collectionsQuery, queryKeys } from "../lib/queries";
import type { KnowledgeCollection } from "../lib/types";

// Optional fields are deliberately capability checks, never guessed from a name.
export type Collection = KnowledgeCollection & {
  kind?: string; source?: string; tier?: "private" | "commons";
  retrieved_share_7d?: number; retrieved_turns_7d?: number; total_turns_7d?: number;
  plugin?: string; ingesting?: { completed: number; total: number };
};
export type CollectionSort = "size" | "name" | "updated" | "share";
export function collectionHref(collection?: string) {
  const url = new URL(window.location.href);
  if (collection) url.searchParams.set("collection", collection);
  else url.searchParams.delete("collection");
  return `${url.pathname}${url.search}${url.hash}`;
}
export function useCollections() {
  const qc = useQueryClient();
  const [collection, setCollection] = useState(() => new URLSearchParams(window.location.search).get("collection") ?? "");
  const [query, setQuery] = useState("");
  const [term, setTerm] = useState("");
  const [all, setAll] = useState(!collection);
  const [sort, setSort] = useState<CollectionSort>("size");
  const [ascending, setAscending] = useState(false);
  useEffect(() => {
    const onPop = (event: PopStateEvent) => {
      // MobileShell dismisses the whole pushed surface on popstate. These entries
      // belong to this browser instead; consume them before that shell listener.
      if (event.state?.knowledgeCollections) event.stopImmediatePropagation();
      const name = new URLSearchParams(window.location.search).get("collection") ?? "";
      setCollection(name); setAll(!name); setQuery(""); setTerm("");
    };
    window.addEventListener("popstate", onPop, true);
    return () => window.removeEventListener("popstate", onPop, true);
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => setTerm(query.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [query]);
  const list = useQuery(collectionsQuery());
  const collections: Collection[] = list.data?.collections ?? [];
  const selected = collections.find(c => c.collection === collection);
  const details = useQuery({
    ...collectionChunksQuery(collection || undefined, "", selected?.chunks ?? 50),
    enabled: !!collection && !!selected && list.data?.enabled !== false,
  });
  const search = useQuery({
    ...collectionsSearchQuery(all ? collections : collections.filter(c => c.collection === collection), term),
    enabled: !!term && list.data?.enabled !== false,
  });
  function open(name = "") {
    const state = { ...window.history.state, knowledgeCollections: true };
    window.history.replaceState(state, "");
    window.history.pushState(state, "", collectionHref(name));
    setCollection(name); setAll(!name); setQuery(""); setTerm("");
  }
  function seedSearch(value: string) { setQuery(value); setTerm(value.trim()); setAll(true); }
  const invalidate = () => qc.invalidateQueries({ queryKey: queryKeys.knowledge });
  const remove = useMutation({
    mutationFn: async (name: string) => {
      // The list aggregates both tiers but bulk writes target only private chunks.
      // Refuse a mixed/shared bucket rather than promise that its whole contents
      // disappear (or mistake a private deletion for stopping fleet sharing).
      const row = collections.find(c => c.collection === name);
      const contents = await qc.fetchQuery(collectionChunksQuery(name, "", row?.chunks ?? 50));
      // The /search route NEVER 500s (knowledge_routes.py logs and returns a 200 with an
      // empty result list on a store error), so an empty read is ambiguous: a genuinely
      // empty bucket, or a failed one. If the list says this collection has chunks but the
      // read came back empty, we can't confirm its tier makeup — refuse rather than fail
      // open and delete a bucket that might hold shared (commons) chunks while reporting
      // them gone. (A network-level failure throws here and already blocks the delete.)
      if (row && row.chunks > 0 && !contents.results.length) {
        throw new Error("Couldn't read this collection's chunks to check for shared entries. Try again.");
      }
      if (contents.results.some(c => c.tier === "commons")) {
        throw new Error("This collection contains shared chunks. Use the individual chunks' Unshare actions.");
      }
      const result = await api.deleteKnowledgeByCollection(name);
      if (!result.enabled || result.error) throw new Error(result.error || "Knowledge is off");
      if (!result.deleted && row && row.chunks > 0) throw new Error("No chunks were removed.");
      return result;
    },
    onSuccess: invalidate,
  });
  const restore = useMutation({
    mutationFn: async (name: string) => {
      const result = await api.restoreKnowledgeByCollection(name);
      if (!result.enabled || result.error || !result.restored) throw new Error(result.error || "No chunks could be restored");
      return result;
    },
    onSuccess: invalidate,
  });
  // No document-level (per-source) delete here: the server's delete-by-source route carries
  // no collection predicate and operator-added chunks all share source="console", so deleting
  // a document from this collection-scoped view would soft-delete same-source chunks in EVERY
  // collection. Whole-document removal is served by Delete collection or the per-chunk delete;
  // a correctly scoped per-document route is a future server card (bd-ihctg owns these routes).
  return { list, collections, selected, details, search, collection, open, query, setQuery, term, all, setAll,
    seedSearch, sort, setSort, ascending, setAscending, remove, restore, invalidate };
}
