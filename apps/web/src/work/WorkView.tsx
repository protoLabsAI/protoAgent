import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { SideNav, Tabs } from "@protolabsai/ui/navigation";
import { useUI } from "../state/uiStore";
import type { SurfaceState } from "../shell/surfaceState";
import { useWorkItems, type WorkItem, type WorkItems } from "./useWorkItems";
import { WorkOverview, workSections } from "./WorkOverview";
import { sections, subscribe, getSnapshot } from "./sectionRegistry";
import "./sections/decisions";
import "./sections/goals";
import "./sections/directives";
import "./sections/tasks";
import "./sections/schedules";
import "./sections/activity";
import "./work-view.css";

export { registerWorkSection, type WorkSectionContext } from "./sectionRegistry";
const emptyState: SurfaceState = {};

export function WorkView({ frame = "rail", sessionId }: { frame?: "rail" | "modal"; sessionId?: string }) {
  useSyncExternalStore(subscribe, getSnapshot);
  const state = useUI((s) => s.surfaceStates.work ?? emptyState);
  const section = state.section ?? "overview";
  const thisChat = typeof state.filters?.thisChat === "boolean" ? state.filters.thisChat : frame === "rail";
  const allSources = useWorkItems({ sessionId, thisChatOnly: false });
  const sources = Object.fromEntries(Object.entries(allSources).map(([key, source]) => [key, {
    ...source, items: source.items.filter((item) => !thisChat || !sessionId || !item.sessionId || item.sessionId === sessionId),
  }])) as WorkItems;
  const items = Object.values(sources).flatMap((source) => source.items);
  const selected = items.find((item) => `${item.kind}:${item.id}` === state.itemId || item.id === state.itemId);
  const selectedKey = selected ? `${selected.kind}:${selected.id}` : state.itemId;
  const otherChatCount = Object.values(allSources).reduce((n, source) => n + source.items.length, 0) - items.length;
  const loading = Object.values(sources).some((source) => source.status === "loading");
  const label = sections.get(section)?.label ?? workSections[section as keyof typeof workSections] ?? section;
  const count = section === "overview" ? items.length : sources[section as keyof WorkItems]?.items.length ?? 0;
  const listRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const returnTo = useRef<string | undefined>(undefined);
  const previousNeeds = useRef<Set<string> | null>(null);
  const [announcement, setAnnouncement] = useState("");
  function update(patch: Partial<SurfaceState>) {
    const current = useUI.getState().surfaceStates.work ?? state;
    useUI.getState().setSurfaceState("work", { ...current, ...patch });
  }
  useEffect(() => {
    if (state.filters?.thisChat === undefined) update({ filters: { ...state.filters, thisChat } });
  }, [thisChat, state.filters]);
  useLayoutEffect(() => {
    if (listRef.current) listRef.current.scrollTop = state.scrollTop ?? 0;
  }, [section, state.itemId, frame, state.scrollTop]);
  useEffect(() => {
    const list = listRef.current;
    if (!list || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (list.getClientRects().length) list.scrollTop = useUI.getState().surfaceStates.work?.scrollTop ?? 0;
    });
    observer.observe(list);
    return () => observer.disconnect();
  }, []);
  const needs = items.filter((item) => item.status === "needs-you").map((item) => `${item.kind}:${item.id}`).sort().join("\n");
  useEffect(() => {
    if (loading) return;
    const next = new Set(needs ? needs.split("\n") : []);
    const added = previousNeeds.current ? [...next].filter((id) => !previousNeeds.current!.has(id)).length : 0;
    previousNeeds.current = next;
    setAnnouncement(`${label}, ${count} items.${added ? ` ${added} new Needs you items.` : ""}`);
  }, [label, count, needs, loading]);
  useLayoutEffect(() => {
    const rows = listRef.current?.querySelectorAll<HTMLElement>("[data-surface-focus]");
    rows?.forEach((row) => row.setAttribute("aria-current", String(row.dataset.surfaceFocus === selectedKey)));
    if (state.itemId) {
      const heading = detailRef.current?.querySelector<HTMLElement>("h2, h3");
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }
    } else if (returnTo.current) {
      Array.from(rows ?? []).find((node) => node.dataset.surfaceFocus === returnTo.current)?.focus({ preventScroll: true });
      returnTo.current = undefined;
    }
  }, [state.itemId, selectedKey, selected?.id, frame]);
  const onBack = () => {
    returnTo.current = selectedKey;
    update({ itemId: undefined });
  };
  const onOpen = (item: WorkItem) => update({ itemId: `${item.kind}:${item.id}`, scrollTop: listRef.current?.scrollTop ?? state.scrollTop });
  const context = { sources, state, onOpen, onBack, sessionId };
  const selectedSection = selected && ({ decision: "decisions", goal: "goals", watch: "watches", task: "tasks", schedule: "schedules", background: "background", activity: "activity", directive: "directives" } as const)[selected.kind];
  const detailRenderer = sections.get(selectedSection ?? section);
  const selectSection = (id: string) => update({ section: id, itemId: undefined, scrollTop: 0 });

  return <div className="work-view" data-frame={frame} data-selected={!!state.itemId}>
    <div className="work-view__header">
      {state.itemId && <Button className="work-view__back" variant="ghost" onClick={onBack}>Back to {label}</Button>}
      <div className="work-view__picker"><SideNav collapsed ariaLabel="Work sections" active={section} onSelect={selectSection}
        items={Object.entries(workSections).map(([id, defaultLabel]) => ({ id, label: sections.get(id)?.label ?? defaultLabel,
          badge: (id === "overview" ? loading : sources[id as keyof WorkItems]?.status === "loading") ? undefined : (id === "overview" ? items.length : sources[id as keyof WorkItems]?.items.length ?? 0),
        }))} /></div>
      <div className="work-view__scope"><Tabs variant="segmented" ariaLabel="Work scope"
        active={thisChat ? "chat" : "all"} items={[{ id: "chat", label: "This chat" }, { id: "all", label: "All" }]}
        onSelect={(id) => update({ filters: { ...state.filters, thisChat: id === "chat" }, itemId: undefined })} /></div>
    </div>
    <div className="work-view__panes">
      <div className="work-view__list" ref={listRef} onScroll={(event) => {
        // Hiding a scroll container can emit a zero scroll event.
        if (event.currentTarget.getClientRects().length) update({ scrollTop: event.currentTarget.scrollTop });
      }}>
        {section === "overview" ? <WorkOverview sources={sources} onOpen={onOpen} onSchedules={() => selectSection("schedules")} otherChatCount={otherChatCount}
          onShowAll={() => update({ filters: { ...state.filters, thisChat: false } })} />
          : sections.get(section)?.render({ ...context, state: { ...state, itemId: undefined } }) ?? <Empty title={`No ${label.toLowerCase()} to show`} />}
      </div>
      <div className="work-view__detail" ref={detailRef}>
        {state.itemId ? <>
          {detailRenderer?.render(context) ?? <Empty title={selected?.title ?? "Item unavailable"} description="Details will appear here." />}</>
          : <Empty title="Pick an item to see its details." />}
      </div>
    </div>
    <div className="work-view__announcement" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
  </div>;
}
