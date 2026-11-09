import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Progress, Skeleton, Spinner, Table, THead, TBody, Tr, Th, Td } from "@protolabsai/ui/data";
import { Input, Select } from "@protolabsai/ui/forms";
import { Stats, Stat } from "@protolabsai/ui/layout";
import { Menu, MenuItem } from "@protolabsai/ui/menu";
import { Accordion, AccordionItem, PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { ConfirmDialog, Tooltip, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Callout, Empty, TextLink } from "@protolabsai/ui/primitives";
import { MoreHorizontal, Search, Trash2 } from "lucide-react";
import { useUI } from "../state/uiStore";
import { ago, errMsg } from "../lib/format";
import type { KnowledgeChunk } from "../lib/types";
import { collectionHref, type Collection, type CollectionSort, type useCollections } from "./useCollections";
import "./collections.css";

type Model = ReturnType<typeof useCollections>;
type RowActions = { onOpen: (name: string) => void; onSearch: (name: string) => void; onDelete: (row: Collection) => void };
const sorts: Record<CollectionSort, string> = { name: "Collection", size: "Size", updated: "Last updated", share: "Share of what the agent reads (7 days)" };
export function sortedCollections(rows: Collection[], sort: CollectionSort, ascending: boolean) {
  return [...rows].sort((a, b) => {
    const cmp = sort === "name" ? a.collection.localeCompare(b.collection)
      : sort === "updated" ? (a.last_ingested_at ?? "").localeCompare(b.last_ingested_at ?? "")
      : sort === "share" ? (a.retrieved_share_7d ?? -1) - (b.retrieved_share_7d ?? -1) : a.chunks - b.chunks;
    return (ascending ? cmp : -cmp) || a.collection.localeCompare(b.collection);
  });
}
function Share({ row }: { row: Collection }) {
  return row.retrieved_share_7d == null ? <span className="kc-muted">Not tracked yet</span> : <>
    <div role="progressbar" aria-label="Share of what the agent reads" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={row.retrieved_share_7d * 100} aria-valuetext={`${Math.round(row.retrieved_share_7d * 100)}%`}>
      <div aria-hidden="true"><Progress status="neutral" value={row.retrieved_share_7d * 100} showValue /></div>
    </div>
    {row.retrieved_turns_7d != null && <span className="kc-muted">used in {row.retrieved_turns_7d} of {row.total_turns_7d ?? "—"} turns, last 7 days</span>}
  </>;
}
function Size({ row }: { row: Collection }) {
  return row.ingesting ? <span><Spinner /> Adding {row.ingesting.completed} of {row.ingesting.total} documents…</span>
    : <span>{row.sources} documents · {row.chunks} chunks</span>;
}
function Metadata({ row }: { row: Collection }) {
  return <>{row.kind && <span className="kc-muted" title={row.source}>{row.kind}{row.source ? ` · ${row.source}` : ""}</span>}
    {row.tier === "commons" && <Badge status="neutral">shared</Badge>}</>;
}
export function CollectionRow({ row, phone = false, onOpen, onSearch, onDelete }: RowActions & { row: Collection; phone?: boolean }) {
  if (phone) return <li><button type="button" className="kc-phone-row" data-collection-name={row.collection} onClick={() => onOpen(row.collection)}>
    <strong>{row.collection}</strong>{row.tier === "commons" && <Badge status="neutral">shared</Badge>}
    {row.ingesting ? <Size row={row} /> : <span className="kc-muted">{row.chunks} chunks · updated {ago(row.last_ingested_at)}</span>}<Share row={row} />
  </button></li>;
  return <Tr onClick={() => onOpen(row.collection)}>
    <Td><TextLink href={collectionHref(row.collection)} data-collection-name={row.collection} onClick={e => { e.preventDefault(); e.stopPropagation(); onOpen(row.collection); }}>{row.collection}</TextLink><Metadata row={row} /></Td>
    <Td><Size row={row} /></Td><Td><Tooltip label={row.last_ingested_at ?? "Unknown"}><span>{ago(row.last_ingested_at)}</span></Tooltip></Td>
    <Td><Share row={row} /></Td>
    <Td onClick={e => e.stopPropagation()}><Menu trigger={<Button icon variant="ghost" aria-label={`Actions for ${row.collection}`}><MoreHorizontal /></Button>}>
      <MenuItem onSelect={() => onOpen(row.collection)}>Open</MenuItem>
      <MenuItem onSelect={() => onSearch(row.collection)}>Search in this collection</MenuItem>
      <MenuItem destructive disabled={!!row.ingesting || !!row.plugin} onSelect={() => onDelete(row)}>{row.tier === "commons" ? "Stop sharing…" : "Delete collection…"}</MenuItem>
    </Menu></Td>
  </Tr>;
}
export function CollectionList({ rows, sort, ascending, onSort, ...actions }: RowActions & {
  rows: Collection[]; sort: CollectionSort; ascending: boolean; onSort: (value: CollectionSort) => void;
}) {
  const sorted = sortedCollections(rows, sort, ascending);
  const dominant = rows.length > 1 ? rows.find(c => c.retrieved_share_7d != null && c.retrieved_share_7d >= .5) : undefined;
  return <>
    {dominant && <Callout tone="info"><strong>{dominant.collection} supplies {Math.round(dominant.retrieved_share_7d! * 100)}% of the knowledge this agent reads.</strong> It was last updated {ago(dominant.last_ingested_at)}. If that's not what you expect, open it to check what's inside. <Button onClick={() => actions.onOpen(dominant.collection)}>Open {dominant.collection}</Button></Callout>}
    <span className="kc-visually-hidden" role="status">Sorted by {sorts[sort].toLowerCase()}, {ascending ? "lowest first" : "highest first"}.</span>
    <div className="kc-desktop"><Table caption="Knowledge collections for this agent" source={rows.some(c => c.retrieved_share_7d != null) ? "Retrieval figures cover the last 7 days." : undefined}>
      <THead><Tr>{(Object.keys(sorts) as CollectionSort[]).map(key => <Th key={key} aria-sort={sort === key ? ascending ? "ascending" : "descending" : "none"}>
        <Button variant="ghost" size="sm" onClick={() => onSort(key)}>{sorts[key]}</Button>
      </Th>)}<Th><span className="kc-visually-hidden">Actions</span></Th></Tr></THead>
      <TBody>{sorted.map(row => <CollectionRow key={row.collection} row={row} {...actions} />)}</TBody>
    </Table></div>
    <div className="kc-phone"><Select aria-label="Sort by" value={sort} onChange={e => onSort(e.target.value as CollectionSort)}>
      {(Object.keys(sorts) as CollectionSort[]).map(key => <option key={key} value={key}>{sorts[key]}</option>)}
    </Select><ul aria-label="Knowledge collections">{sorted.map(row => <CollectionRow key={row.collection} row={row} phone {...actions} />)}</ul></div>
  </>;
}
// Chunks with no `source` share this bucket; it has no document identity of its own.
const SOURCELESS = "Entries without a source";
// Only document-identifying sources get this action; manually added and sourceless
// entries remain individually editable and deletable.
export function isDeletableDocument(source: string) {
  return source !== SOURCELESS && source !== "console";
}
function DocumentChunks({ source, chunks, renderCard, onDeleteDocument }: {
  source: string; chunks: KnowledgeChunk[]; renderCard: (chunk: KnowledgeChunk) => ReactNode;
  onDeleteDocument?: (source: string, count: number) => void;
}) {
  const [limit, setLimit] = useState(50);
  return <>
    {onDeleteDocument && isDeletableDocument(source) && <div className="kc-document-actions">
      <Button variant="ghost" size="sm" aria-label={`Delete document ${source}`} onClick={() => onDeleteDocument(source, chunks.length)}>
        <Trash2 size={14} /> Delete document
      </Button></div>}
    <ul className="playbook-list">{chunks.slice(0, limit).map(renderCard)}</ul>
    {chunks.length > limit && <Button onClick={() => setLimit(n => n + 50)}>Show the next 50</Button>}</>;
}
export function CollectionDetail({ row, chunks, renderCard, onDelete, onDeleteDocument, pendingReview = false }: {
  row: Collection; chunks: KnowledgeChunk[]; renderCard: (chunk: KnowledgeChunk) => ReactNode;
  onDelete: (row: Collection) => void; onDeleteDocument?: (source: string, count: number) => void; pendingReview?: boolean;
}) {
  const documents = new Map<string, KnowledgeChunk[]>();
  for (const chunk of chunks) {
    const source = chunk.source || SOURCELESS;
    documents.set(source, [...(documents.get(source) ?? []), chunk]);
  }
  return <>
    <Stats><Stat label="Share of what the agent reads" value={row.retrieved_share_7d == null ? <span className="kc-muted">Not tracked yet</span> : `${Math.round(row.retrieved_share_7d * 100)}%`} />
      <Stat label="Used in" value={row.retrieved_turns_7d == null ? <span className="kc-muted">Not tracked yet</span> : `${row.retrieved_turns_7d} of ${row.total_turns_7d ?? "—"} turns`} />
      <Stat label="Size" value={<Size row={row} />} /><Stat label="Last updated" value={<Tooltip label={row.last_ingested_at ?? "Unknown"}><span>{ago(row.last_ingested_at)}</span></Tooltip>} /></Stats>
    {row.ingesting && <Size row={row} />}
    {!chunks.length ? (pendingReview
      ? <Empty title="Nothing awaiting review here." description="Entries the agent wrote start pending; confirm or reject them as they arrive." />
      : <Empty title="This collection is empty." action={!row.plugin && <Button disabled={!!row.ingesting} title={row.ingesting ? "Wait until it finishes adding." : undefined} onClick={() => onDelete(row)}>{row.tier === "commons" ? "Stop sharing…" : "Delete collection…"}</Button>} />) : <>
      <h3>Documents</h3><Accordion>{[...documents].sort(([a], [b]) => a.localeCompare(b)).map(([source, members]) =>
        <AccordionItem key={source} title={`${source} · ${members.length} chunks`} defaultOpen={documents.size === 1}>
          <DocumentChunks source={source} chunks={[...members].sort((a, b) => (a.position ?? a.id) - (b.position ?? b.id))} renderCard={renderCard} onDeleteDocument={onDeleteDocument} />
        </AccordionItem>)}</Accordion></>}
  </>;
}
export function highlight(text: string, query: string): ReactNode {
  const words = query.trim().split(/\s+/).filter(Boolean).map(s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!words.length) return text;
  const parts = text.split(new RegExp(`(${words.join("|")})`, "gi"));
  return parts.map((part, i) => i % 2 ? <mark key={i}>{part}</mark> : part);
}
export function KnowledgeSearch({ model }: { model: Model }) {
  return <div className="kc-search">
    <Input id="knowledge-collection-search" type="search" icon={<Search />} aria-label="Search knowledge"
      placeholder={model.collection && !model.flat && !model.all ? `Search in ${model.collection}…` : "Search all knowledge…"}
      value={model.query} onChange={e => model.setQuery(e.target.value)} onClear={() => model.setQuery("")} />
    {model.collection && !model.flat && <Tabs variant="segmented" ariaLabel="Search in" active={model.all ? "all" : "collection"}
      items={[{ id: "all", label: "All collections" }, { id: "collection", label: model.collection }]} onSelect={id => model.setAll(id === "all")} />}
  </div>;
}
function CollectionLoading({ detail }: { detail: boolean }) {
  return <div aria-label="Loading collections">{detail ? <>
    <Stats>{["Share of what the agent reads", "Used in", "Size", "Last updated"].map(label => <Stat key={label} label={label} value={<Skeleton />} />)}</Stats>
    <Skeleton lines={3} /><Skeleton lines={3} />
  </> : <>
    <div className="kc-desktop"><Table caption="Knowledge collections for this agent">
      <THead><Tr>{Object.values(sorts).map(label => <Th key={label}>{label}</Th>)}<Th>Actions</Th></Tr></THead>
      <TBody>{[0, 1, 2].map(i => <Tr key={i}><Td colSpan={5}><Skeleton /></Td></Tr>)}</TBody>
    </Table></div>
    <div className="kc-phone"><Skeleton lines={3} /><Skeleton lines={3} /><Skeleton lines={3} /></div>
  </>}</div>;
}
export function CollectionsBrowser({ model, renderCard, onAdd, onSettings }: {
  model: Model; renderCard: (chunk: KnowledgeChunk) => ReactNode; onAdd: () => void; onSettings: () => void;
}) {
  const [pending, setPending] = useState<Collection | null>(null);
  const [pendingDoc, setPendingDoc] = useState<{ source: string; count: number; collection: string } | null>(null);
  const toast = useToast();
  const focusAfterDelete = useRef<string | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const scroll = useRef(0);
  const searchScroll = useRef(0);
  const wasSearching = useRef(false);
  const { list, selected: listed, collection, details, search, term } = model;
  const selected = listed && details.data?.results.some(c => c.tier === "commons")
    ? { ...listed, tier: "commons" as const } : listed;
  const sorted = sortedCollections(model.collections, model.sort, model.ascending);
  const previousCollection = useRef(collection);
  useEffect(() => {
    const body = contentRef.current?.closest(".stage-body");
    if (previousCollection.current && !collection && body) body.scrollTop = scroll.current;
    previousCollection.current = collection;
  }, [collection]);
  useEffect(() => {
    const body = contentRef.current?.closest(".stage-body");
    if (!body) return;
    if (term && !wasSearching.current) searchScroll.current = body.scrollTop;
    if (!term && wasSearching.current) body.scrollTop = searchScroll.current;
    wasSearching.current = !!term;
  }, [term]);
  useEffect(() => {
    if (focusAfterDelete.current === null || collection || pending) return;
    const name = focusAfterDelete.current;
    const timer = window.setTimeout(() => {
      const target = Array.from(contentRef.current?.querySelectorAll<HTMLElement>("[data-collection-name]") ?? []).find(el => el.dataset.collectionName === name && el.getClientRects().length);
      (target ?? document.getElementById("knowledge-collection-search"))?.focus();
      focusAfterDelete.current = null;
    }, 0);
    return () => window.clearTimeout(timer);
  }, [collection, pending, model.collections]);
  function open(name: string) {
    const body = contentRef.current?.closest(".stage-body");
    if (!collection && body) scroll.current = body.scrollTop;
    model.open(name);
    requestAnimationFrame(() => { if (body) body.scrollTop = name ? 0 : scroll.current; });
  }
  function requestDelete(row: Collection) { model.remove.reset(); setPending(row); }
  async function remove() {
    if (!pending || model.remove.isPending) return;
    const row = pending;
    // The bulk route only touches private chunks. Commons must retain the existing
    // per-chunk Unshare path until a reversible, tier-aware route exists.
    if (row.tier === "commons") { setPending(null); return; }
    try {
      const result = await model.remove.mutateAsync(row.collection);
      const index = sorted.findIndex(c => c.collection === row.collection);
      focusAfterDelete.current = sorted[index + 1]?.collection ?? sorted[index - 1]?.collection ?? "";
      setPending(null); model.open();
      toast({ tone: "success", title: "Knowledge", duration: 10000, message: <span>Deleted {row.collection} ({result.deleted} chunks). <Button variant="ghost" size="sm" onClick={async () => {
        try { await model.restore.mutateAsync(row.collection); toast({ tone: "success", title: "Knowledge", message: `Restored ${row.collection}.` }); }
        catch (error) { toast({ tone: "error", title: "Couldn't restore collection", message: errMsg(error) }); }
      }}>Undo</Button></span> });
    } catch { /* Keep the dialog and its inline retry visible. */ }
  }
  function requestDeleteDoc(source: string, count: number) { model.removeSource.reset(); setPendingDoc({ source, count, collection }); }
  async function removeDoc() {
    if (!pendingDoc || model.removeSource.isPending) return;
    const { source, collection: documentCollection } = pendingDoc;
    const document = { source, collection: documentCollection };
    try {
      const result = await model.removeSource.mutateAsync(document);
      setPendingDoc(null);
      toast({ tone: "success", title: "Knowledge", duration: 10000, message: <span>Deleted {result.deleted} chunk{result.deleted === 1 ? "" : "s"} from “{source}”. <Button variant="ghost" size="sm" onClick={async () => {
        try { await model.restoreSource.mutateAsync(document); toast({ tone: "success", title: "Knowledge", message: `Restored “${source}”.` }); }
        catch (error) { toast({ tone: "error", title: "Couldn't restore document", message: errMsg(error) }); }
      }}>Undo</Button></span> });
    } catch { /* Keep the dialog and its inline retry visible. */ }
  }
  const actions: RowActions = { onOpen: open, onDelete: requestDelete, onSearch: name => {
    open(name); requestAnimationFrame(() => document.getElementById("knowledge-collection-search")?.focus());
  } };
  if (model.flat) return <div className="kc-browser" ref={contentRef}>
    <PanelHeader title="Knowledge" />
    {collection && <TextLink href={collectionHref()} onClick={e => { e.preventDefault(); open(""); }}>Back to all knowledge</TextLink>}
    <KnowledgeSearch model={model} />
    {model.fallback.isPending ? <Skeleton lines={3} />
      : model.fallback.error ? <Alert status="warning">Couldn't load knowledge. <Button onClick={() => void model.fallback.refetch()}>Retry</Button></Alert>
      : model.fallback.data?.enabled === false ? <Empty title="Knowledge is off for this agent" description="Turn it on to let the agent read collections you add." action={<Button onClick={onSettings}>Open settings</Button>} />
      : <>
        {term && <p role="status" aria-live="polite">{model.fallback.data?.results.length ?? 0} matches</p>}
        {model.fallback.data?.results.length
          ? <ul className="playbook-list">{model.fallback.data.results.map(renderCard)}</ul>
          : term ? <Empty description={`Nothing in this agent's knowledge matches "${term}".`} />
          : model.pendingOnly ? <Empty title="Nothing awaiting review here." />
          : <Empty title="No knowledge yet" description="Add a folder, a file or a web page and it becomes a collection this agent can read." action={<Button onClick={onAdd}>Add a source</Button>} />}
      </>}
  </div>;
  const loading = list.isPending || (!!collection && !!selected && details.isPending);
  const failed = list.error || (selected && details.error);
  return <div className="kc-browser" ref={contentRef}>
    {collection && <TextLink href={collectionHref()} aria-label="Back to all collections" onClick={e => { e.preventDefault(); open(""); }}>← All collections</TextLink>}
    <PanelHeader title={collection || "Collections"} kicker={list.isPending ? undefined : collection
      ? <><Metadata row={selected ?? { collection, chunks: 0, sources: 0, last_ingested_at: null }} />{selected?.tier === "commons" && " Shared with every agent on this box."}</>
      : `${model.collections.length} collections · ${model.collections.reduce((n, c) => n + c.chunks, 0)} chunks`}
      actions={selected && (selected.plugin ? <span className="kc-muted">Managed by {selected.plugin}. Turn the plugin's docs off in its settings to remove it. <Button variant="ghost" onClick={() => useUI.getState().openPluginConfig(selected.plugin!, selected.plugin!)}>Open plugin settings</Button></span>
        : <Button variant="danger" size="sm" disabled={!!selected.ingesting} title={selected.ingesting ? "Wait until it finishes adding." : undefined} onClick={() => requestDelete(selected)}>{selected.tier === "commons" ? "Stop sharing…" : "Delete collection…"}</Button>)} />
    <KnowledgeSearch model={model} />
    {loading ? <CollectionLoading detail={!!collection} />
      : failed ? <Alert status="warning">Couldn't load {collection || "collections"}. The agent still has its knowledge; only this view failed to load. <Button onClick={() => { void list.refetch(); if (collection) void details.refetch(); }}>Retry</Button></Alert>
      : list.data?.enabled === false ? <Empty title="Knowledge is off for this agent" description="Turn it on to let the agent read collections you add." action={<Button onClick={onSettings}>Open settings</Button>} />
      : <>{term && (search.isPending ? <Skeleton lines={3} /> : search.error ? <Alert status="warning">Couldn't search knowledge. <Button onClick={() => void search.refetch()}>Retry</Button></Alert>
      : <><p role="status" aria-live="polite">{search.data?.results.length ?? 0} matches{model.all ? ` in ${new Set(search.data?.results.map(c => c.collection)).size} collections` : ` in ${collection}`}</p>
        {!search.data?.results.length ? <Empty description={model.all ? `Nothing in this agent's knowledge matches "${term}".` : `Nothing in ${collection} matches "${term}".`}
          action={!model.all && <Button onClick={() => model.setAll(true)}>Search all collections</Button>} />
          : <ul className="playbook-list">{search.data.results.map(renderCard)}</ul>}</>)}
      <div hidden={!!term}>{collection ? !selected ? <Callout tone="info">This collection isn't here any more. It may have been deleted. <Button onClick={() => open("")}>Back to all collections</Button></Callout>
        : <CollectionDetail key={collection} row={selected} chunks={details.data?.results ?? []} renderCard={renderCard} onDelete={requestDelete} onDeleteDocument={requestDeleteDoc} pendingReview={model.pendingOnly} />
      : !model.collections.length ? <Empty title="No knowledge yet" description="Add a folder, a file or a web page and it becomes a collection this agent can read." action={<Button onClick={onAdd}>Add a source</Button>} />
      : <CollectionList rows={model.collections} sort={model.sort} ascending={model.ascending} onSort={key => {
        model.setAscending(key === model.sort ? !model.ascending : key === "name"); model.setSort(key);
      }} {...actions} />}</div></>}
    <ConfirmDialog open={pending !== null} destructive title={pending?.tier === "commons" ? `Stop sharing ${pending.collection}?` : `Delete ${pending?.collection}?`}
      confirmLabel={pending?.tier === "commons" ? "Understood" : model.remove.isPending ? <><Spinner /> Deleting…</> : "Delete collection"} cancelLabel="Keep it" onConfirm={() => void remove()}
      onClose={() => { if (!model.remove.isPending) setPending(null); }}>
      <div className="kc-delete-body">
      {pending?.tier === "commons" ? <Alert status="warning">Collection-wide unsharing is not available yet. Use the individual chunks' Unshare actions; each agent's private copy stays.</Alert>
        : <p>This removes all {pending?.chunks} chunks in {pending?.sources} documents from this agent's knowledge. It stops reading them from its next turn. You can undo this for a short time afterwards.</p>}
      {pending?.retrieved_share_7d != null && <p>This collection supplied {Math.round(pending.retrieved_share_7d * 100)}% of the knowledge it read in the last 7 days.</p>}
      {model.remove.error && <Alert status="warning">Couldn't delete {pending?.collection}. Nothing was removed. {errMsg(model.remove.error)} <Button onClick={() => void remove()}>Try again</Button></Alert>}
      </div>
    </ConfirmDialog>
    <ConfirmDialog open={pendingDoc !== null} destructive title="Delete this document?"
      confirmLabel={model.removeSource.isPending ? <><Spinner /> Deleting…</> : "Delete document"} cancelLabel="Keep it" onConfirm={() => void removeDoc()}
      onClose={() => { if (!model.removeSource.isPending) setPendingDoc(null); }}>
      <div className="kc-delete-body">
      <p>This removes all {pendingDoc?.count} chunk{pendingDoc?.count === 1 ? "" : "s"} of “{pendingDoc?.source}” from “{pendingDoc?.collection}”. It stops reading them from its next turn. You can undo this for a short time afterwards.</p>
      {model.removeSource.error && <Alert status="warning">Couldn't delete “{pendingDoc?.source}”. Nothing was removed. {errMsg(model.removeSource.error)} <Button onClick={() => void removeDoc()}>Try again</Button></Alert>}
      </div>
    </ConfirmDialog>
  </div>;
}
