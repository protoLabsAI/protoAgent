import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { Button } from "@protolabsai/ui/primitives";
import { X } from "lucide-react";
import { WorkEmpty as Empty } from "./WorkEmpty";
import { WorkHeading } from "./WorkHeading";
import { useSurfaceFrame } from "../shell/SurfaceFrame";
import { SideNav } from "@protolabsai/ui/navigation";
import { useToast, Tooltip } from "@protolabsai/ui/overlays";
import { api } from "../lib/api";
import { sessionFromDurableTurns, SESSION_TURN_LIMIT } from "../chat/sessionHydration";
import { openView } from "../app/palette/nav";
import { chatStore } from "../chat/chat-store";
import { useKbIntents } from "../keybindings/intents";
import { isConnected, onConnectionChange } from "../lib/events";
import { useDecisions } from "../decisions/useDecisions";
import { decisionMeta, fromDecision, relTime, type GroupableWorkItem, type WorkGroupId } from "./tone";
import type { DecisionRow } from "../lib/api/decisions";
import type { InboxItem } from "../lib/types";
import { useUI } from "../state/uiStore";
import type { SurfaceState } from "../shell/surfaceState";
import { useWorkItems, type WorkItems } from "./useWorkItems";
import { workGroups } from "./workGroups";
import { WorkDetail } from "./WorkDetail";
import { WorkOverview, WorkGroupSection, groupSources, itemKey, workSections } from "./WorkOverview";
import { sections, subscribe, getSnapshot } from "./sectionRegistry";
import { useGoalCreator } from "./sections/goals";
import "./sections/decisions";
import "./sections/goals";
import "./sections/directives";
import "./sections/tasks";
import "./sections/schedules";
import "./work-view.css";

export { registerWorkSection, type WorkSectionContext } from "./sectionRegistry";
export { workSections };
const emptyState: SurfaceState = {};

const GROUP_IDS: readonly WorkGroupId[] = ["needs-you", "doing-now", "coming-up", "instructions"];

/** Legacy per-source section ids land on the group that shows their rows, mirroring the
 *  built-in registry annotations so the map can't drift while both exist (S4c removes it). */
const LEGACY_GROUPS: Record<string, WorkGroupId> = {
  decisions: "needs-you", goals: "doing-now", watches: "coming-up", tasks: "doing-now",
  schedules: "coming-up", background: "doing-now", directives: "instructions",
};

/** The canonical place a saved or deep-linked section opens: a group id passes through; a
 *  legacy or extension section resolves to its registered group; history (activity) and
 *  anything unknown land on the Overview. */
export function canonicalSection(section: string): "overview" | WorkGroupId {
  if (section === "overview" || (GROUP_IDS as readonly string[]).includes(section)) return section as "overview" | WorkGroupId;
  return sections.get(section)?.group ?? LEGACY_GROUPS[section] ?? "overview";
}

/** The shared record to show over Work's row, if any: a settled state is one-way, so a row Work
 *  already has as settled keeps it over a shared record still cached as pending. */
function sharedOverride(row: DecisionRow | undefined, shared: DecisionRow | undefined): DecisionRow | undefined {
  if (!shared) return undefined;
  return row && row.state !== "pending" && shared.state === "pending" ? undefined : shared;
}

/** The element that actually scrolls the Work list. In the rail and modal that's the list
 *  itself; in the phone sheet the list grows to its content and the Drawer body scrolls, so
 *  saving and restoring the list's own scrollTop silently did nothing there. */
function listScroller(list: HTMLElement): HTMLElement {
  for (let node: HTMLElement | null = list; node; node = node.parentElement) {
    const overflow = getComputedStyle(node).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && node.scrollHeight > node.clientHeight + 1) return node;
  }
  return list;
}

/** Hosts the shared goal creator where the palette's New goal intent now lands: the intent's
 *  legacy `goals` section resolves to Doing now, and `filters.create` opens the dialog. A
 *  child component keeps the creator's queries out of panes that never need them. */
function GoalCreatorHost({ state }: { state: SurfaceState }) {
  return <>{useGoalCreator(state).creator}</>;
}

/** An undelivered page (§2 "Delivery") — no section renderer owns the inbox, so the
 *  detail renders here from the normalized row. */
function DeliveryDetail({ item }: { item: GroupableWorkItem }) {
  const row = item.raw as InboxItem;
  return <WorkDetail
    title={item.title || "Delivery"}
    status="needs-you"
    fields={[
      { label: "From", value: row.source || null },
      { label: "Waiting since", value: row.created_at ? relTime(row.created_at).text : null },
    ]}
    actions={[]}
    technical={{ Id: String(row.id), "Dedup key": row.dedup_key ?? "" }}
    type="Delivery"
  />;
}

export function WorkView({ frame = "rail", sessionId }: { frame?: "rail" | "modal"; sessionId?: string }) {
  const hostFrame = useSurfaceFrame() ?? frame;
  useSyncExternalStore(subscribe, getSnapshot);
  const state = useUI((s) => s.surfaceStates.work ?? emptyState);
  const section = canonicalSection(state.section ?? "overview");
  const thisChatOnly = state.filters?.thisChat === true;
  const toast = useToast();
  const agentOnline = useSyncExternalStore(onConnectionChange, isConnected);
  const decisions = useDecisions();
  const allSources = { ...useWorkItems({ sessionId, thisChatOnly: false }) };
  // Work lists every decision (history too); the shared store holds the answer path's records.
  // A settled state is final, so whichever copy has settled wins; otherwise the shared record
  // wins. A row the server reported gone (404) is dropped. Work's own list catches up on
  // reconnect (useWorkItems), so neither copy needs to be authoritative over the other.
  allSources.decisions = { ...allSources.decisions, items: allSources.decisions.items.filter((item) => !decisions.missing[item.id]) };
  const selectedDecisionId = state.itemId?.startsWith("decision:") ? state.itemId.slice(9) : undefined;
  const retained = selectedDecisionId ? decisions.records[selectedDecisionId] : undefined;
  if (retained && retained.state !== "pending" && !allSources.decisions.items.some((item) => item.id === retained.id)) {
    // Keep the selected terminal record visible after the pending source refreshes.
    allSources.decisions = { ...allSources.decisions, items: [...allSources.decisions.items, {
      kind: "decision", id: retained.id, raw: retained, title: retained.question,
      status: fromDecision(retained.state), meta: decisionMeta(retained, true), sessionId: retained.session_id,
    }] };
  }
  const sources = Object.fromEntries(Object.entries(allSources).map(([key, source]) => [key, {
    ...source, items: source.items.map((item) => {
      const record = item.kind === "decision" ? sharedOverride(item.raw as DecisionRow | undefined, decisions.records[item.id]) : undefined;
      return record ? { ...item, raw: record, status: fromDecision(record.state), meta: decisionMeta(record, true) } : item;
    }),
  }])) as WorkItems;
  // The shared selector is the ONLY row/count projection (spec §7.1): the Overview, a picked
  // group and the picker badges all read it, with Needs you never chat-filtered.
  const groups = workGroups(sources, { thisChatOnly, sessionId });
  const group = groups.find((candidate) => candidate.id === section);
  const total = groups.reduce((sum, candidate) => sum + candidate.count, 0);
  const items = [...groups.flatMap((candidate) => candidate.items),
    ...Object.values(sources).flatMap((source) => source.items)];
  const selected = items.find((item) => itemKey(item) === state.itemId || item.id === state.itemId);
  const selectedKey = selected ? itemKey(selected) : state.itemId;
  const loading = Object.values(sources).some((source) => source.status === "loading");
  const groupLoading = (id: WorkGroupId) => groupSources[id].some((kind) => sources[kind].status === "loading");
  const label = group?.label ?? "Overview";
  const count = group ? group.count : total;
  const listRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const listSize = useRef<{ width: number; height: number } | null>(null);
  const returnTo = useRef<string | undefined>(undefined);
  const previousNeeds = useRef<Set<string> | null>(null);
  const [announcement, setAnnouncement] = useState("");
  function update(patch: Partial<SurfaceState>) {
    const current = useUI.getState().surfaceStates.work ?? state;
    useUI.getState().setSurfaceState("work", { ...current, ...patch });
  }
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const scroller = listScroller(list);
    // When an ancestor scrolls (the phone sheet), the detail replaces the list inside that same
    // scroller — restore it only while the list is the thing showing.
    if (scroller === list || list.getClientRects().length) scroller.scrollTop = state.scrollTop ?? 0;
  }, [section, state.itemId, hostFrame, state.scrollTop]);
  useEffect(() => {
    const list = listRef.current;
    if (!list || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      listSize.current = { width: list.clientWidth, height: list.clientHeight };
      // Only when the list is its own scroller (rail, modal): there the saved position tracks
      // every scroll. In the phone sheet the Drawer body scrolls and the saved value only moves
      // on open, so restoring it on a resize would yank the reader back (#5075 review).
      if (list.getClientRects().length && listScroller(list) === list) {
        list.scrollTop = useUI.getState().surfaceStates.work?.scrollTop ?? 0;
      }
    });
    observer.observe(list);
    return () => observer.disconnect();
  }, []);
  const needs = groups.find((candidate) => candidate.id === "needs-you")!.items.map(itemKey).sort().join("\n");
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
  }, [state.itemId, selectedKey, selected?.id, hostFrame]);
  const onBack = () => {
    returnTo.current = selectedKey;
    update({ itemId: undefined });
  };
  useEffect(() => {
    if (!state.itemId || hostFrame === "rail") return;
    // The DS samples Escape in capture and dismisses at window bubble. Document bubble
    // lets React confirmations consume it first, and also covers the Dialog's header.
    const onEscape = (event: KeyboardEvent) => {
      const dialog = listRef.current?.closest('[role="dialog"]');
      if (event.key !== "Escape" || event.defaultPrevented || !dialog
        || !(event.target instanceof Element) || event.target.closest('[role="dialog"]') !== dialog) return;
      event.preventDefault();
      event.stopPropagation();
      onBack();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [state.itemId, selectedKey, hostFrame]);
  const onOpen = (item: GroupableWorkItem) => update({
    itemId: itemKey(item),
    scrollTop: listRef.current ? listScroller(listRef.current).scrollTop : state.scrollTop,
  });
  const openSourceChat = async (sourceSessionId: string, reply = false) => {
    try {
      if (!chatStore.getSnapshot().sessions.some((session) => session.id === sourceSessionId)) {
        const { turns, live_task_id } = await api.chatSessionTurns(sourceSessionId, SESSION_TURN_LIMIT);
        const session = sessionFromDurableTurns({ session_id: sourceSessionId,
          last_updated: turns[0]?.last_updated ?? null, turn_count: turns.length }, turns, live_task_id);
        if (!session) throw new Error("The source chat is unavailable.");
        chatStore.restoreDismissedSession(sourceSessionId);
        const token = chatStore.captureHydrationEligibility(sourceSessionId);
        if (token) chatStore.hydrateSessions([session], [token]);
      }
      if (!chatStore.getSnapshot().sessions.some((session) => session.id === sourceSessionId)) {
        throw new Error("The source chat is unavailable.");
      }
    } catch {
      toast({ tone: "error", title: "Couldn't open source chat", message: "Try again when the agent is connected." });
      return;
    }
    chatStore.switchSession(sourceSessionId);
    useUI.getState().closeSurfaceModal();
    openView("chat");
    if (reply) useKbIntents.getState().focusComposer();
  };
  const context = { sources, state: { ...state, itemId: selectedKey ?? state.itemId }, onOpen, onBack, sessionId,
    decisionActions: { agentOnline, openSourceChat, answerDecision: decisions.answer } };
  const selectedSection = selected && ({ decision: "decisions", goal: "goals", watch: "watches", task: "tasks",
    schedule: "schedules", background: "background", directive: "directives", wait: "schedules", delivery: "delivery", activity: "activity" } as const)[selected.kind];
  const detailRenderer = selectedSection ? sections.get(selectedSection) : undefined;
  const selectSection = (id: string) => update({ section: id, itemId: undefined, scrollTop: 0 });

  return <div className="work-view" data-frame={hostFrame} data-selected={!!state.itemId} onKeyDown={(event) => {
    // Portalled overlays bubble through React too; only handle events inside this view.
    if (event.key !== "Escape" || event.defaultPrevented || !state.itemId
      || !event.currentTarget.contains(event.target as Node)) return;
    event.preventDefault();
    event.stopPropagation();
    onBack();
  }}>
    <div className="work-view__header">
      {state.itemId && <Button className="work-view__back" size={hostFrame === "sheet" ? "md" : "sm"} variant="ghost" onClick={onBack}>Back to {label}</Button>}
      <div className="work-view__picker"><SideNav collapsed ariaLabel="Work sections" active={section} onSelect={selectSection}
        items={[
          { id: "overview", label: "Overview", badge: loading ? undefined : total },
          ...groups.map((candidate) => ({ id: candidate.id, label: candidate.label,
            badge: groupLoading(candidate.id) ? undefined : candidate.count })),
        ]} /></div>
    </div>
    <div className="work-view__panes">
      <div className="work-view__list" ref={listRef} onScroll={(event) => {
        const list = event.currentTarget;
        const size = listSize.current;
        // WebKit can deliver the hide/reset scroll event after the list is visible again,
        // before ResizeObserver restores its saved position. Do not persist that reset.
        if (list.getClientRects().length && (!size || (size.width === list.clientWidth && size.height === list.clientHeight))) {
          update({ scrollTop: list.scrollTop });
        }
      }}>
        <div className="work-view__content">
          {section !== "overview" && group && <WorkHeading label={group.label} count={group.count} />}
          {section === "overview" || !group
            ? <WorkOverview groups={groups} sources={sources} onOpen={onOpen} />
            : <WorkGroupSection group={group} sources={sources} onOpen={onOpen} />}
        </div>
      </div>
      {state.itemId && <div className="work-view__detail" ref={detailRef}>
        <div className="work-view__close"><Tooltip label="Close details"><Button icon variant="ghost" size="sm" aria-label="Close details" onClick={onBack}><X aria-hidden="true" /></Button></Tooltip></div>
        {selected?.kind === "delivery" ? <DeliveryDetail item={selected} />
          : detailRenderer?.render(context) ?? <Empty title={selected?.title ?? "Item unavailable"} description="Details will appear here." />}
      </div>}
    </div>
    {state.filters?.create === true && <GoalCreatorHost state={state} />}
    <div className="work-view__announcement" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
  </div>;
}
