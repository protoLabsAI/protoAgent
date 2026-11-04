import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Folder, Plus, Settings, Star } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from "react";

import { Menu, MenuFilter, MenuGroup, MenuItem, MenuLabel, MenuSeparator, type MenuHandle } from "@protolabsai/ui/menu";
import { Drawer, Tooltip } from "@protolabsai/ui/overlays";
import { Input } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";

import { agentHref, api, currentSlug } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { readKey, writeKey } from "../lib/storage";
import type { FleetAgent, FleetStatus } from "../lib/types";
import { useIsMobile } from "../lib/useIsMobile";
import { fleetSettingsDisabledReason } from "./fleetSettingsGate";

// The URL slug is the agent's STABLE id, never its (editable) display name — renaming an agent
// must not change its URL/bookmarks. `host` is the reserved slug for this instance. Group/pin
// writes and recents key off the immutable `id` everywhere, same rule as slugs.
const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);
const nameOf = (a: FleetAgent) => a.label ?? a.name;

// Spec: docs/design/fleet-switcher-groups.md. The filter appears at ≥ 10 agents; recents/collapsed
// live per browser (through the ADR 0114 storage seam); pins/groups live with the hub roster. The
// recents key is deliberately NOT the spec's `protoagent.fleet.recent`: that one is already the
// palette's legacy slug→timestamp store with a single documented writer (fleetPalette.ts), so the
// switcher keeps its own key rather than corrupting a shared one.
const FILTER_MIN = 10;
const RECENT_KEY = "protoagent.fleet.switcherRecent"; // up to 3 agent ids, newest first
const COLLAPSED_KEY = "protoagent.fleet.collapsed"; // group ids + "remote" that are collapsed
const REMOTE_KEY = "remote"; // the single automatic "Remote" section's collapse/home key

function readIds(key: string): string[] {
  try {
    const raw = readKey("local", key);
    const value = raw ? JSON.parse(raw) : [];
    return Array.isArray(value) ? value.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}
function writeIds(key: string, ids: string[]) {
  // Best-effort: a full / disabled store never blocks a switch — the roster read stays authoritative.
  writeKey("local", key, JSON.stringify(ids));
}

type HomeSection = { key: string; name: string; agents: FleetAgent[] };

// Topbar / mobile-shell agent switcher (ADR 0042 slug routing + the fleet-switcher spec). The
// focused agent lives in the URL (/app/agent/<slug>/), so picking one NAVIGATES there — each window
// is its own agent, a reload can't desync. The dropdown ALWAYS shows (New agent + Fleet settings
// stay reachable even with a single agent); only a hard fleet-API error falls back to the plain
// name. Desktop is the DS Menu (filter, collapsible groups, pins, recents, a per-row actions menu);
// below 767px (`useIsMobile`) it's a DS bottom-sheet Drawer with the same sections and copy.
export function FleetSwitcher({
  fallbackName,
  onNewAgent,
  onManageFleet,
}: {
  fallbackName: ReactNode;
  onNewAgent?: () => void;
  onManageFleet?: () => void;
}) {
  const qc = useQueryClient();
  // Poll so the switcher reflects the fleet live (a newly-added agent, a pin from another browser).
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false, refetchInterval: 3_000 });
  const isMobile = useIsMobile();

  const agents = useMemo(() => fleet.data?.agents ?? [], [fleet.data]);
  const groups = useMemo(() => fleet.data?.groups ?? [], [fleet.data]);
  const slug = currentSlug(); // the agent THIS window is on
  const current = agents.find((a) => slugOf(a) === slug);
  // Non-null only on a spawned member reached DIRECTLY on its own port (#1708/#1999): there is no
  // hub roster to write to, so the row actions (pin / move) are hidden there, like Fleet settings.
  const fleetSettingsBlocked = fleetSettingsDisabledReason(agents);
  const canWrite = !fleetSettingsBlocked;

  const [filter, setFilter] = useState("");
  const [collapsed, setCollapsed] = useState<string[]>(() => readIds(COLLAPSED_KEY));
  // Read once: a switch navigates (full page load), so recents are re-read on the next mount.
  const [recent] = useState<string[]>(() => readIds(RECENT_KEY));

  const query = filter.trim().toLowerCase();
  const filtering = query.length > 0;

  const setCollapsedState = useCallback((next: string[]) => {
    setCollapsed(next);
    writeIds(COLLAPSED_KEY, next);
  }, []);
  const toggleCollapsed = useCallback(
    (key: string) => {
      if (filtering) return; // groups are held open while filtering
      setCollapsedState(collapsed.includes(key) ? collapsed.filter((k) => k !== key) : [...collapsed, key]);
    },
    [collapsed, filtering, setCollapsedState],
  );
  const isCollapsed = (key: string) => !filtering && collapsed.includes(key);

  // ── roster writes (hub, shared by every browser) ──────────────────────────────────────────
  // Optimistic cache patch so the row lands at once; the 3s poll / invalidation stays the source
  // of truth and rolls back a rejected write (a 400 on a blank/duplicate/reserved group name).
  const patchAgent = useCallback(
    (id: string, patch: Partial<FleetAgent>) =>
      qc.setQueryData<FleetStatus>(queryKeys.fleet, (cur) =>
        cur ? { ...cur, agents: cur.agents.map((a) => (a.id === id ? { ...a, ...patch } : a)) } : cur,
      ),
    [qc],
  );
  const invalidateFleet = useCallback(() => qc.invalidateQueries({ queryKey: queryKeys.fleet }), [qc]);
  const pinMut = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) => (pinned ? api.pinFleetAgent(id) : api.unpinFleetAgent(id)),
    onSettled: invalidateFleet,
  });
  const moveMut = useMutation({
    mutationFn: ({ id, group }: { id: string; group: string | null }) => api.setFleetAgentGroup(id, group),
    onSettled: invalidateFleet,
  });
  const createGroupMut = useMutation({ mutationFn: (name: string) => api.createFleetGroup(name), onSettled: invalidateFleet });

  const togglePin = useCallback(
    (a: FleetAgent) => {
      const pinned = !a.pinned;
      patchAgent(a.id, { pinned, pinned_at: pinned ? new Date().toISOString() : undefined });
      pinMut.mutate({ id: a.id, pinned });
    },
    [patchAgent, pinMut],
  );
  const moveToGroup = useCallback(
    (a: FleetAgent, group: string | null) => {
      patchAgent(a.id, { group });
      if (group) setCollapsedState(collapsed.filter((k) => k !== group)); // show where it lands
      moveMut.mutate({ id: a.id, group });
    },
    [patchAgent, collapsed, setCollapsedState, moveMut],
  );
  // 1–40 chars, trimmed, unique ignoring case; "Remote" is reserved. Copy verbatim from the spec.
  const validateGroupName = useCallback(
    (raw: string): string | null => {
      const name = raw.trim();
      if (!name) return "Give the group a name.";
      if (name.toLowerCase() === "remote") return '"Remote" is reserved for agents on paired machines.';
      if (groups.some((g) => g.name.toLowerCase() === name.toLowerCase())) return `There's already a group called "${name}".`;
      return null;
    },
    [groups],
  );
  const createAndMove = useCallback(
    async (a: FleetAgent, raw: string): Promise<string | null> => {
      const err = validateGroupName(raw);
      if (err) return err;
      const res = await createGroupMut.mutateAsync(raw.trim());
      moveToGroup(a, res.group.id);
      return null;
    },
    [validateGroupName, createGroupMut, moveToGroup],
  );

  const doSwitch = useCallback(
    (a: FleetAgent) => {
      const target = slugOf(a);
      if (target === slug) return; // already here
      // Written BEFORE navigating (spec): newest first, deduped, capped at 3.
      writeIds(RECENT_KEY, [a.id, ...recent.filter((x) => x !== a.id)].slice(0, 3));
      window.location.href = agentHref(target);
    },
    [slug, recent],
  );

  // ── sections ───────────────────────────────────────────────────────────────────────────────
  // One home per agent — Ungrouped, an operator group, or Remote — in switcher order; Current,
  // Pinned and Recent are shortcuts that duplicate the home row on purpose (spec Decision 3).
  const groupIds = useMemo(() => new Set(groups.map((g) => g.id)), [groups]);
  const homeOf = useCallback(
    (a: FleetAgent): string => (a.remote ? REMOTE_KEY : a.group && groupIds.has(a.group) ? a.group : "ungrouped"),
    [groupIds],
  );
  const sections = useMemo<HomeSection[]>(() => {
    const base: HomeSection[] = [
      { key: "ungrouped", name: "", agents: [] },
      ...groups.map((g) => ({ key: g.id, name: g.name, agents: [] as FleetAgent[] })),
      { key: REMOTE_KEY, name: "Remote", agents: [] },
    ];
    const byKey = new Map(base.map((s) => [s.key, s]));
    for (const a of agents) byKey.get(homeOf(a))?.agents.push(a);
    return base;
  }, [agents, groups, homeOf]);

  const pinned = useMemo(
    () => agents.filter((a) => a.pinned).sort((x, y) => (x.pinned_at ?? "").localeCompare(y.pinned_at ?? "")),
    [agents],
  );
  const recentAgents = useMemo(() => {
    const pinnedIds = new Set(pinned.map((a) => a.id));
    return recent
      .map((id) => agents.find((a) => a.id === id))
      .filter((a): a is FleetAgent => !!a && a.id !== current?.id && !pinnedIds.has(a.id))
      .slice(0, 3);
  }, [recent, agents, pinned, current]);

  const matches = useCallback(
    (a: FleetAgent, groupName: string) =>
      [nameOf(a), a.name, groupName, a.remote ? a.machine ?? "" : ""].join(" ").toLowerCase().includes(query),
    [query],
  );
  // The first match in rendered order (ungrouped, groups, Remote) — Enter in the filter switches to it.
  const topMatch = useMemo(() => {
    if (!filtering) return null;
    for (const s of sections) {
      const m = s.agents.find((a) => matches(a, s.name));
      if (m) return m;
    }
    return null;
  }, [filtering, sections, matches]);
  const onFilterSubmit = useCallback(() => {
    if (topMatch) doSwitch(topMatch);
  }, [topMatch, doSwitch]);

  const showFilter = agents.length >= FILTER_MIN;

  // ── desktop row actions menu (right-click on a row): Pin, Move to group…, New group… ──
  // A separate DS Menu driven imperatively by the row's context-menu. It swaps between a root /
  // move / new-group view in place (MenuItem onSelect preventing default keeps the menu open —
  // the same trick the DS MenuGroup uses). Hidden entirely in the blocked member window.
  const rowMenu = useRef<RowMenuHandle>(null);

  // On desktop the filter takes focus when the menu opens, so the operator can just start typing
  // (the DS MenuFilter isn't a menu item, so Radix's open-focus lands on the first row, not it).
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => {
    if (!menuOpen || isMobile || !showFilter || typeof requestAnimationFrame !== "function") return;
    const id = requestAnimationFrame(() => {
      (document.querySelector(".pl-menu:not(.fleet-rowmenu) .pl-menu__filter input") as HTMLElement | null)?.focus();
    });
    return () => cancelAnimationFrame(id);
  }, [menuOpen, isMobile, showFilter]);

  // Only a hard fleet-API error hides the switcher; otherwise it's always available.
  if (fleet.isError) return <>{fallbackName}</>;

  // ── row rendering ─────────────────────────────────────────────────────────────────────────
  const agentRow = (a: FleetAgent, section: string) => {
    const isCurrent = slugOf(a) === slug;
    return (
      <MenuItem
        key={`${section}:${a.id}`}
        icon={<StatusDot status={a.running ? "success" : "neutral"} pulse={a.running} />}
        onSelect={() => doSwitch(a)}
        action={
          canWrite
            ? {
                icon: (
                  <Star size={14} className={`fleet-star${a.pinned ? " is-pinned" : ""}`} fill={a.pinned ? "currentColor" : "none"} />
                ),
                label: a.pinned ? `Unpin ${nameOf(a)}` : `Pin ${nameOf(a)} to top`,
                onClick: () => togglePin(a),
              }
            : undefined
        }
      >
        <span
          className="fleet-switcher-name"
          onContextMenu={canWrite ? (e) => rowMenu.current?.open(e, a) : undefined}
        >
          <span className="fleet-switcher-name-text">{nameOf(a)}</span>
          {a.host ? <Badge status="neutral">this instance</Badge> : null}
          {a.remote && a.machine ? <Badge status="neutral">{a.machine}</Badge> : null}
          {isCurrent ? <Check size={14} className="fleet-switcher-check" aria-label="current agent" /> : null}
        </span>
      </MenuItem>
    );
  };

  const groupHeader = (s: HomeSection): ReactNode => {
    const anyRunning = s.agents.some((a) => a.running);
    return (
      <span className="fleet-group-label">
        <span className="fleet-group-label-text">{s.name}</span>
        {isCollapsed(s.key) && anyRunning ? <StatusDot status="success" /> : null}
      </span>
    );
  };

  // Build the ordered menu body, inserting a separator before each non-first section.
  const desktopBody: ReactNode[] = [];
  let sepId = 0;
  const pushSep = () => {
    if (desktopBody.length) desktopBody.push(<MenuSeparator key={`sep-${sepId++}`} />);
  };
  if (showFilter) {
    desktopBody.push(
      <MenuFilter
        key="filter"
        value={filter}
        onValueChange={setFilter}
        onSubmit={onFilterSubmit}
        placeholder="Find an agent"
        aria-label="Find an agent by name or group"
      />,
    );
  }
  if (!filtering) {
    if (current) desktopBody.push(agentRow(current, "current"));
    if (pinned.length) {
      pushSep();
      desktopBody.push(<MenuLabel key="pinned-label">Pinned</MenuLabel>, ...pinned.map((a) => agentRow(a, "pinned")));
    }
    if (recentAgents.length) {
      pushSep();
      desktopBody.push(<MenuLabel key="recent-label">Recent</MenuLabel>, ...recentAgents.map((a) => agentRow(a, "recent")));
    }
  }
  let matchCount = 0;
  for (const s of sections) {
    const shown = filtering ? s.agents.filter((a) => matches(a, s.name)) : s.agents;
    if (!shown.length) continue; // empty sections (and empty groups) are hidden
    matchCount += shown.length;
    pushSep();
    if (s.key === "ungrouped") {
      desktopBody.push(...shown.map((a) => agentRow(a, s.key)));
      continue;
    }
    desktopBody.push(
      <MenuGroup
        key={`group-${s.key}`}
        label={groupHeader(s)}
        count={s.agents.length}
        collapsed={filtering ? false : collapsed.includes(s.key)}
        onCollapsedChange={() => toggleCollapsed(s.key)}
      >
        {shown.map((a) => agentRow(a, s.key))}
      </MenuGroup>,
    );
  }
  if (filtering && matchCount === 0) {
    desktopBody.push(
      <MenuItem key="no-match" disabled>
        No agents match "{filter.trim()}"
      </MenuItem>,
    );
  }
  pushSep();
  desktopBody.push(...footerItems({ fleetSettingsBlocked, onNewAgent, onManageFleet }));

  // ── phone bottom sheet ──────────────────────────────────────────────────────────────────────
  const triggerLabel = current ? nameOf(current) : fallbackName;

  if (isMobile) {
    return (
      <PhoneSwitcher
        triggerLabel={triggerLabel}
        showFilter={showFilter}
        filter={filter}
        setFilter={setFilter}
        onFilterSubmit={onFilterSubmit}
        filtering={filtering}
        matches={matches}
        current={current}
        pinned={pinned}
        recentAgents={recentAgents}
        sections={sections}
        slug={slug}
        isCollapsed={isCollapsed}
        toggleCollapsed={toggleCollapsed}
        collapsed={collapsed}
        canWrite={canWrite}
        groups={groups}
        doSwitch={doSwitch}
        togglePin={togglePin}
        moveToGroup={moveToGroup}
        createAndMove={createAndMove}
        fleetSettingsBlocked={fleetSettingsBlocked}
        onNewAgent={onNewAgent}
        onManageFleet={onManageFleet}
      />
    );
  }

  return (
    <>
      <Menu
        align="start"
        onOpenChange={(open) => {
          setMenuOpen(open);
          if (!open) setFilter(""); // the filter text is never saved
        }}
        trigger={
          <button type="button" className="fleet-switcher-trigger" data-testid="fleet-switcher" aria-label="Switch agent">
            <span className="fleet-switcher-trigger-name">{triggerLabel}</span>
            <ChevronDown size={14} aria-hidden />
          </button>
        }
      >
        {desktopBody}
      </Menu>
      {canWrite ? (
        <RowMenu ref={rowMenu} groups={groups} togglePin={togglePin} moveToGroup={moveToGroup} createAndMove={createAndMove} />
      ) : null}
    </>
  );
}

// Footer rows (New agent · Fleet settings), gated exactly as before (#1708/#1999): on a directly
// reached member they are disabled with a tooltip saying WHERE fleet settings live, not hidden.
function footerItems({
  fleetSettingsBlocked,
  onNewAgent,
  onManageFleet,
}: {
  fleetSettingsBlocked: string | null;
  onNewAgent?: () => void;
  onManageFleet?: () => void;
}): ReactNode[] {
  return [
    fleetSettingsBlocked ? (
      // The DS Tooltip's wrapper span is the hover target — a disabled Radix menu item is
      // pointer-events:none, so the item itself can't fire the tooltip.
      <Tooltip key="new" label={fleetSettingsBlocked} side="left">
        <MenuItem icon={<Plus size={14} />} disabled>
          New agent
        </MenuItem>
      </Tooltip>
    ) : (
      <MenuItem key="new" icon={<Plus size={14} />} onSelect={() => onNewAgent?.()}>
        New agent
      </MenuItem>
    ),
    fleetSettingsBlocked ? (
      <Tooltip key="settings" label={fleetSettingsBlocked} side="left">
        <MenuItem icon={<Settings size={14} />} disabled>
          Fleet settings
        </MenuItem>
      </Tooltip>
    ) : (
      <MenuItem key="settings" icon={<Settings size={14} />} onSelect={() => onManageFleet?.()}>
        Fleet settings
      </MenuItem>
    ),
  ];
}

// ─────────────────────────────────────────────────────────────────────────────────────────────
// The shared desktop row-actions menu. Driven imperatively from a row's context-menu through the
// ref handle below; one instance per switcher serves every row. It swaps a root / move / new-group
// view in place.
type RowMenuHandle = { open: (e: { preventDefault: () => void; clientX: number; clientY: number }, a: FleetAgent) => void };

const RowMenu = forwardRef<
  RowMenuHandle,
  {
    groups: { id: string; name: string }[];
    togglePin: (a: FleetAgent) => void;
    moveToGroup: (a: FleetAgent, group: string | null) => void;
    createAndMove: (a: FleetAgent, name: string) => Promise<string | null>;
  }
>(function RowMenu({ groups, togglePin, moveToGroup, createAndMove }, handle) {
  const ref = useRef<MenuHandle>(null);
  const [target, setTarget] = useState<FleetAgent | null>(null);
  const [view, setView] = useState<"root" | "move" | "new">("root");
  const [newName, setNewName] = useState("");
  const [newErr, setNewErr] = useState<string | null>(null);

  useImperativeHandle(handle, () => ({
    open: (e, a) => {
      e.preventDefault();
      setTarget(a);
      setView("root");
      setNewName("");
      setNewErr(null);
      ref.current?.open({ x: e.clientX, y: e.clientY });
    },
  }));

  // Keeps the menu open after a navigation item: Radix only closes when onSelect is NOT
  // defaultPrevented. The DS MenuItem types onSelect as `() => void`, but Radix passes the event
  // through at runtime (the DS MenuGroup header relies on the same), so we receive and prevent it.
  const keepOpen = (fn: () => void) =>
    ((e: Event) => {
      e.preventDefault();
      fn();
    }) as unknown as () => void;

  let body: ReactNode = null;
  if (target && view === "root") {
    body = (
      <>
        <MenuItem onSelect={() => togglePin(target)}>{target.pinned ? "Unpin" : "Pin to top"}</MenuItem>
        {target.remote ? (
          <Tooltip label="Remote agents are grouped by their machine." side="left">
            <MenuItem icon={<Folder size={14} />} disabled>
              Move to group…
            </MenuItem>
          </Tooltip>
        ) : (
          <MenuItem icon={<Folder size={14} />} onSelect={keepOpen(() => setView("move"))}>
            Move to group…
          </MenuItem>
        )}
      </>
    );
  } else if (target && view === "move") {
    body = (
      <>
        {groups.map((g) => (
          <MenuItem key={g.id} onSelect={() => moveToGroup(target, g.id)}>
            <span className="fleet-move-row">
              <span>{g.name}</span>
              {target.group === g.id ? <Check size={14} className="fleet-switcher-check" aria-label="current group" /> : null}
            </span>
          </MenuItem>
        ))}
        <MenuItem onSelect={() => moveToGroup(target, null)}>
          <span className="fleet-move-row">
            <span>No group</span>
            {!target.group || !groups.some((g) => g.id === target.group) ? (
              <Check size={14} className="fleet-switcher-check" aria-label="current group" />
            ) : null}
          </span>
        </MenuItem>
        <MenuSeparator />
        <MenuItem icon={<Plus size={14} />} onSelect={keepOpen(() => setView("new"))}>
          New group…
        </MenuItem>
      </>
    );
  } else if (target && view === "new") {
    body = (
      <div className="fleet-newgroup" onKeyDown={(e) => e.stopPropagation() /* keep Radix typeahead off the field */}>
        <Input
          value={newName}
          placeholder="Group name"
          aria-label="New group name"
          maxLength={40}
          autoFocus
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => {
            setNewName(e.target.value);
            setNewErr(null);
          }}
          onKeyDown={async (e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              const err = await createAndMove(target, newName);
              if (err) setNewErr(err);
            } else if (e.key === "Escape") {
              e.preventDefault();
              setView("move");
            }
          }}
        />
        {newErr ? (
          <div className="fleet-newgroup-err" role="alert">
            {newErr}
          </div>
        ) : (
          <div className="fleet-newgroup-hint">Enter to create · Escape to cancel</div>
        )}
      </div>
    );
  }

  return (
    <Menu
      ref={ref}
      className="fleet-rowmenu"
      onOpenChange={(open) => {
        if (!open) {
          setView("root");
          setTarget(null);
        }
      }}
    >
      {body}
    </Menu>
  );
});

// ─────────────────────────────────────────────────────────────────────────────────────────────
// Phone: the switcher is a DS bottom-sheet Drawer (side="bottom", a fixed 85dvh so it doesn't jump
// as the filter narrows the list); the row actions are a SECOND sheet that REPLACES it (one sheet,
// one focus trap). The filter is fixed at the top and does not take focus on open; the list is the
// only scroll container; nothing calls scrollIntoView and the background is scroll-locked by the DS
// overlay layer — so opening a sheet never scrolls the page behind it.
type PhoneProps = {
  triggerLabel: ReactNode;
  showFilter: boolean;
  filter: string;
  setFilter: (v: string) => void;
  onFilterSubmit: () => void;
  filtering: boolean;
  matches: (a: FleetAgent, groupName: string) => boolean;
  current?: FleetAgent;
  pinned: FleetAgent[];
  recentAgents: FleetAgent[];
  sections: HomeSection[];
  slug: string;
  isCollapsed: (key: string) => boolean;
  toggleCollapsed: (key: string) => void;
  collapsed: string[];
  canWrite: boolean;
  groups: { id: string; name: string }[];
  doSwitch: (a: FleetAgent) => void;
  togglePin: (a: FleetAgent) => void;
  moveToGroup: (a: FleetAgent, group: string | null) => void;
  createAndMove: (a: FleetAgent, name: string) => Promise<string | null>;
  fleetSettingsBlocked: string | null;
  onNewAgent?: () => void;
  onManageFleet?: () => void;
};

function PhoneSwitcher(props: PhoneProps) {
  const [sheet, setSheet] = useState<"none" | "switcher" | "actions">("none");
  const [actTarget, setActTarget] = useState<FleetAgent | null>(null);
  const [actView, setActView] = useState<"root" | "move" | "new">("root");
  const [newName, setNewName] = useState("");
  const [newErr, setNewErr] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const closeSheets = useCallback(() => {
    setSheet("none");
    setActView("root");
  }, []);
  const backToSwitcher = useCallback(() => {
    setSheet("switcher");
    setActView("root");
  }, []);
  const openActions = useCallback((a: FleetAgent) => {
    setActTarget(a);
    setActView("root");
    setNewName("");
    setNewErr(null);
    setSheet("actions"); // replaces the switcher sheet — one sheet at a time
  }, []);

  // Move focus INTO the switcher sheet with preventScroll (spec: never scroll the page behind the
  // sheet), onto the list rather than the filter (a phone must not pop the keyboard on open). Runs
  // after the DS focus-trap's initial focus (child effect) because this is the parent.
  useEffect(() => {
    if (sheet === "switcher") listRef.current?.focus({ preventScroll: true });
  }, [sheet]);

  const { current, pinned, recentAgents, sections, slug, filtering, matches } = props;

  const phoneRow = (a: FleetAgent, section: string) => (
    <PhoneRow
      key={`${section}:${a.id}`}
      a={a}
      isCurrent={slugOf(a) === slug}
      canWrite={props.canWrite}
      onSwitch={() => {
        props.doSwitch(a);
        closeSheets();
      }}
      onMore={() => openActions(a)}
    />
  );

  const list: ReactNode[] = [];
  if (!filtering) {
    if (current) list.push(phoneRow(current, "current"));
    if (pinned.length) {
      list.push(
        <div className="fleet-slabel pl-menu__group-label" key="pinned-label">
          Pinned
        </div>,
        ...pinned.map((a) => phoneRow(a, "pinned")),
      );
    }
    if (recentAgents.length) {
      list.push(
        <div className="fleet-slabel pl-menu__group-label" key="recent-label">
          Recent
        </div>,
        ...recentAgents.map((a) => phoneRow(a, "recent")),
      );
    }
  }
  let matchCount = 0;
  for (const s of sections) {
    const shown = filtering ? s.agents.filter((a) => matches(a, s.name)) : s.agents;
    if (!shown.length) continue;
    matchCount += shown.length;
    if (s.key === "ungrouped") {
      list.push(...shown.map((a) => phoneRow(a, s.key)));
      continue;
    }
    const open = !props.isCollapsed(s.key);
    const anyRunning = s.agents.some((a) => a.running);
    list.push(
      <button
        type="button"
        key={`ghead-${s.key}`}
        className="fleet-ghead"
        aria-expanded={open}
        aria-label={`${s.name}, ${s.agents.length} agents, ${open ? "expanded" : "collapsed"}`}
        onClick={() => props.toggleCollapsed(s.key)}
      >
        <ChevronRight size={16} className={`fleet-chev${open ? " is-open" : ""}`} aria-hidden />
        <span className="fleet-ghead-name">{s.name}</span>
        {!open && anyRunning ? <StatusDot status="success" /> : null}
        <Badge status="neutral">{s.agents.length}</Badge>
      </button>,
    );
    if (open) list.push(...shown.map((a) => phoneRow(a, s.key)));
  }
  if (filtering && matchCount === 0) {
    list.push(
      <div className="fleet-prow is-disabled" key="no-match" aria-disabled>
        No agents match "{props.filter.trim()}"
      </div>,
    );
  }

  const act = actTarget;
  return (
    <>
      <button
        type="button"
        className="fleet-switcher-trigger"
        data-testid="fleet-switcher"
        aria-label="Switch agent"
        aria-haspopup="dialog"
        aria-expanded={sheet !== "none"}
        onClick={() => setSheet("switcher")}
      >
        <span className="fleet-switcher-trigger-name">{props.triggerLabel}</span>
        <ChevronDown size={14} aria-hidden />
      </button>

      <Drawer open={sheet === "switcher"} onClose={closeSheets} side="bottom" size="85dvh" title="Switch agent" className="fleet-sheet">
        <div className="fleet-sheet-inner">
          {props.showFilter ? (
            <MenuFilter
              value={props.filter}
              onValueChange={props.setFilter}
              onSubmit={props.onFilterSubmit}
              placeholder="Find an agent"
              aria-label="Find an agent by name or group"
            />
          ) : null}
          <div className="fleet-sheet-list" ref={listRef} tabIndex={-1}>
            {list}
            <div className="fleet-sheet-foot">
              <FooterButtons
                fleetSettingsBlocked={props.fleetSettingsBlocked}
                onNewAgent={() => {
                  closeSheets();
                  props.onNewAgent?.();
                }}
                onManageFleet={() => {
                  closeSheets();
                  props.onManageFleet?.();
                }}
              />
            </div>
          </div>
        </div>
      </Drawer>

      <Drawer
        open={sheet === "actions"}
        onClose={() => (actView === "root" ? backToSwitcher() : setActView("root"))}
        side="bottom"
        className="fleet-act-sheet"
        title={
          <span className="fleet-act-title">
            {actView !== "root" ? (
              <button type="button" className="fleet-act-back" aria-label="Back" onClick={() => setActView(actView === "new" ? "move" : "root")}>
                <ArrowLeft size={16} aria-hidden /> Back
              </button>
            ) : null}
            <span>{act ? nameOf(act) : ""}</span>
          </span>
        }
      >
        {act && actView === "root" ? (
          <div className="fleet-act-body" role="menu" aria-label={`${nameOf(act)} actions`}>
            <button
              type="button"
              className="fleet-prow fleet-act-item"
              onClick={() => {
                props.togglePin(act);
                backToSwitcher();
              }}
            >
              <Star size={16} fill={act.pinned ? "currentColor" : "none"} aria-hidden /> {act.pinned ? "Unpin" : "Pin to top"}
            </button>
            {act.remote ? (
              <>
                <div className="fleet-prow is-disabled" aria-disabled>
                  <Folder size={16} aria-hidden /> Move to group…
                </div>
                <p className="fleet-act-note">Remote agents are grouped by their machine.</p>
              </>
            ) : (
              <button type="button" className="fleet-prow fleet-act-item" onClick={() => setActView("move")}>
                <Folder size={16} aria-hidden /> Move to group…
                <ChevronRight size={16} className="fleet-act-sub" aria-hidden />
              </button>
            )}
          </div>
        ) : null}

        {act && actView === "move" ? (
          <div className="fleet-act-body" role="menu" aria-label="Move to group">
            {props.groups.map((g) => (
              <button
                type="button"
                key={g.id}
                className="fleet-prow fleet-act-item"
                onClick={() => {
                  props.moveToGroup(act, g.id);
                  backToSwitcher();
                }}
              >
                <span className="fleet-ghead-name">{g.name}</span>
                {act.group === g.id ? <Check size={16} className="fleet-switcher-check" aria-label="current group" /> : null}
              </button>
            ))}
            <button
              type="button"
              className="fleet-prow fleet-act-item"
              onClick={() => {
                props.moveToGroup(act, null);
                backToSwitcher();
              }}
            >
              <span className="fleet-ghead-name">No group</span>
              {!act.group || !props.groups.some((g) => g.id === act.group) ? (
                <Check size={16} className="fleet-switcher-check" aria-label="current group" />
              ) : null}
            </button>
            <button type="button" className="fleet-prow fleet-act-item" onClick={() => setActView("new")}>
              <Plus size={16} aria-hidden /> New group…
            </button>
          </div>
        ) : null}

        {act && actView === "new" ? (
          <div className="fleet-newgroup fleet-act-body">
            <div className="fleet-newgroup-row">
              <Input
                value={newName}
                placeholder="Group name"
                aria-label="New group name"
                maxLength={40}
                autoFocus
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => {
                  setNewName(e.target.value);
                  setNewErr(null);
                }}
                onKeyDown={async (e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    const err = await props.createAndMove(act, newName);
                    if (err) setNewErr(err);
                    else backToSwitcher();
                  }
                }}
              />
              <Button
                type="button"
                size="sm"
                onClick={async () => {
                  const err = await props.createAndMove(act, newName);
                  if (err) setNewErr(err);
                  else backToSwitcher();
                }}
              >
                Create
              </Button>
            </div>
            {newErr ? (
              <div className="fleet-newgroup-err" role="alert">
                {newErr}
              </div>
            ) : null}
          </div>
        ) : null}
      </Drawer>
    </>
  );
}

function FooterButtons({
  fleetSettingsBlocked,
  onNewAgent,
  onManageFleet,
}: {
  fleetSettingsBlocked: string | null;
  onNewAgent?: () => void;
  onManageFleet?: () => void;
}) {
  return (
    <>
      <button type="button" className="fleet-prow fleet-foot-item" disabled={!!fleetSettingsBlocked} onClick={() => onNewAgent?.()}>
        <Plus size={16} aria-hidden /> New agent
      </button>
      <button
        type="button"
        className="fleet-prow fleet-foot-item"
        disabled={!!fleetSettingsBlocked}
        title={fleetSettingsBlocked ?? undefined}
        onClick={() => onManageFleet?.()}
      >
        <Settings size={16} aria-hidden /> Fleet settings
      </button>
      {fleetSettingsBlocked ? <p className="fleet-act-note">{fleetSettingsBlocked}</p> : null}
    </>
  );
}

// A 44px phone agent row: StatusDot + name + badges + current check, a filled-star marker when
// pinned, and an always-visible ⋯ actions button (44×44). Long-press is a shortcut to the same
// actions, never the only way.
function PhoneRow({
  a,
  isCurrent,
  canWrite,
  onSwitch,
  onMore,
}: {
  a: FleetAgent;
  isCurrent: boolean;
  canWrite: boolean;
  onSwitch: () => void;
  onMore: () => void;
}) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suppress = useRef(false);
  const clear = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  return (
    <div className="fleet-prow-wrap">
      <button
        type="button"
        className="fleet-prow"
        onClick={() => {
          if (suppress.current) {
            suppress.current = false;
            return;
          }
          onSwitch();
        }}
        onPointerDown={() => {
          if (!canWrite) return;
          timer.current = setTimeout(() => {
            timer.current = null;
            suppress.current = true;
            onMore();
          }, 500);
        }}
        onPointerUp={clear}
        onPointerLeave={clear}
        onPointerCancel={clear}
      >
        <StatusDot status={a.running ? "success" : "neutral"} pulse={a.running} />
        <span className="fleet-switcher-name">
          <span className="fleet-switcher-name-text">{nameOf(a)}</span>
          {a.host ? <Badge status="neutral">this instance</Badge> : null}
          {a.remote && a.machine ? <Badge status="neutral">{a.machine}</Badge> : null}
          {isCurrent ? <Check size={16} className="fleet-switcher-check" aria-label="current agent" /> : null}
        </span>
      </button>
      {canWrite ? (
        <>
          {a.pinned ? (
            <span className="fleet-pinmark" aria-hidden>
              <Star size={14} fill="currentColor" />
            </span>
          ) : null}
          <button type="button" className="fleet-more" aria-label={`More for ${nameOf(a)}`} onClick={onMore}>
            ⋯
          </button>
        </>
      ) : null}
    </div>
  );
}
