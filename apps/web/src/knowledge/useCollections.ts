import { useEffect, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { collectionChunksQuery, collectionsSearchQuery, collectionsQuery, knowledgeQuery, queryKeys } from "../lib/queries";
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
  // "Pending review" queue (ADR 0108 D7): narrow the browsed chunks / search to rows
  // awaiting an operator verdict. Kept on this surface — the agent still delivers
  // pending rows, so the confirm/reject controls stay reachable here until the Memory
  // inspector owns them. Its own trailing cache segment, so toggling never relabels.
  const [pendingOnly, setPendingOnly] = useState(false);
  const reviewState = pendingOnly ? ("pending" as const) : undefined;
  useEffect(() => {
    const onPop = (event: PopStateEvent) => {
      // MobileShell dismisses the whole pushed surface on popstate. These entries
      // belong to this browser instead; consume them before that shell listener.
      if (event.state?.knowledgeCollections) event.stopImmediatePropagation();
      const name = new URLSearchParams(window.location.search).get("collection") ?? "";
      setCollection(name); setAll(!name); setQuery(""); setTerm(""); setPendingOnly(false);
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
  // list_collections is optional in ADR 0031. An empty listing also covers
  // unsupported/failed aggregation, so check the original unscoped API before
  // claiming the agent has no knowledge. Never infer membership from domain.
  const flat = list.isSuccess && list.data.enabled && !collections.length;
  const fallback = useQuery({
    ...knowledgeQuery(term, reviewState),
    placeholderData: keepPreviousData,
    enabled: flat,
  });
  const selected = collections.find(c => c.collection === collection);
  const details = useQuery({
    ...collectionChunksQuery(collection || undefined, "", selected?.chunks ?? 50, reviewState),
    enabled: !!collection && !!selected && list.data?.enabled !== false,
  });
  const search = useQuery({
    ...collectionsSearchQuery(all ? collections : collections.filter(c => c.collection === collection), term, reviewState),
    // keepPreviousData (ADR 0013): a search-as-you-type surface, so each new term keeps
    // the prior results on screen while the next load runs instead of blanking to a skeleton.
    placeholderData: keepPreviousData,
    enabled: !!term && !flat && list.data?.enabled !== false,
  });
  function open(name = "") {
    const state = { ...window.history.state, knowledgeCollections: true };
    window.history.replaceState(state, "");
    window.history.pushState(state, "", collectionHref(name));
    setCollection(name); setAll(!name); setQuery(""); setTerm(""); setPendingOnly(false);
  }
  function seedSearch(value: string) { setQuery(value); setTerm(value.trim()); setAll(true); setPendingOnly(false); }
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
  // Capture both identifiers so Undo retains its scope after navigation.
  const removeSource = useMutation({
    mutationFn: async ({ source, collection }: { source: string; collection: string }) => {
      if (!collection.trim()) throw new Error("A collection is required");
      const result = await api.deleteKnowledgeBySource(source, collection);
      if (!result.enabled || result.error) throw new Error(result.error || "Knowledge is off");
      // Report the server's count, never the local one: a zero means nothing was removed.
      if (!result.deleted) throw new Error("No chunks were removed.");
      return result;
    },
    onSuccess: invalidate,
  });
  const restoreSource = useMutation({
    mutationFn: async ({ source, collection }: { source: string; collection: string }) => {
      if (!collection.trim()) throw new Error("A collection is required");
      const result = await api.restoreKnowledgeBySource(source, collection);
      if (!result.enabled || result.error || !result.restored) throw new Error(result.error || "No chunks could be restored");
      return result;
    },
    onSuccess: invalidate,
  });
  return { list, flat, fallback, collections, selected, details, search, collection, open, query, setQuery, term, all, setAll,
    seedSearch, sort, setSort, ascending, setAscending, pendingOnly, setPendingOnly, remove, restore,
    removeSource, restoreSource, invalidate };
}
