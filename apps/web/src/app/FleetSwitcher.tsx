import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Folder, Plus, Settings, Star } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";

import { Menu, MenuFilter, MenuGroup, MenuItem, MenuLabel, MenuSeparator, type MenuHandle } from "@protolabsai/ui/menu";
import { Drawer, Tooltip, useToast } from "@protolabsai/ui/overlays";
import { Input } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";

import { agentHref, api, currentSlug } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { readKey, writeKey } from "../lib/storage";
import type { FleetAgent, FleetStatus } from "../lib/types";
import { useIsMobile } from "../lib/useIsMobile";
import { fleetHasGroupsApi, fleetSettingsDisabledReason, fleetWriteErrorMessage } from "./fleetSettingsGate";

// The URL slug is the agent's STABLE id, never its (editable) display name — renaming an agent
// must not change its URL/bookmarks. `host` is the reserved slug for this instance. Group/pin
// writes key off the immutable `id` everywhere, same rule as slugs.
const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);
const nameOf = (a: FleetAgent) => a.label ?? a.name;

// Collapse state lives per browser; pins/groups live with the hub roster.
// Old switcher recents are deliberately ignored.
const FILTER_MIN = 10;
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
// name. Desktop is the DS Menu (filter, collapsible groups, pins, a per-row actions menu);
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
  const toast = useToast();
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
  // A hub that predates the groups/pins API (#4731) returns the flat roster with no `groups` field:
  // its pin/group routes 404/405, so the write controls (star, Pin, Move) are hidden there too and
  // the plain Ungrouped + Remote list shows (groups is []). The filter stays available.
  // Once the roster has loaded (fleet.data present) and carried no `groups`, it's a
  // legacy hub; while still loading we don't yet know, so don't flash the controls off.
  const groupsSupported = !fleet.data || fleetHasGroupsApi(fleet.data);
  const canWrite = !fleetSettingsBlocked && groupsSupported;

  const [filter, setFilter] = useState("");
  const [collapsed, setCollapsed] = useState<string[]>(() => readIds(COLLAPSED_KEY));
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
  // Optimistically patch one agent and keep the pre-write roster so a REJECTED pin/move can roll the
  // row back at once — the 3s poll would also restore it, but not before the operator sees the wrong
  // state stick (the silent-failure this card fixes). Cancels the in-flight poll first so a fetch
  // that started before the write can't resolve over the patch.
  const optimisticAgent = useCallback(
    async (id: string, patch: Partial<FleetAgent>): Promise<{ prev?: FleetStatus }> => {
      await qc.cancelQueries({ queryKey: queryKeys.fleet });
      const prev = qc.getQueryData<FleetStatus>(queryKeys.fleet);
      patchAgent(id, patch);
      return { prev };
    },
    [qc, patchAgent],
  );
  // A rejected pin/move: put the roster back AND tell the operator — never a silent revert on the
  // next poll. A name the hub rejected carries its own copy; anything else gets the generic copy.
  const onWriteError = useCallback(
    (e: unknown, ctx: { prev?: FleetStatus } | undefined) => {
      if (ctx?.prev) qc.setQueryData(queryKeys.fleet, ctx.prev);
      toast({ tone: "error", message: fleetWriteErrorMessage(e) });
    },
    [qc, toast],
  );
  const pinMut = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) => (pinned ? api.pinFleetAgent(id) : api.unpinFleetAgent(id)),
    onMutate: ({ id, pinned }) => optimisticAgent(id, { pinned, pinned_at: pinned ? new Date().toISOString() : undefined }),
    onError: (e, _v, ctx) => onWriteError(e, ctx),
    onSettled: invalidateFleet,
  });
  const moveMut = useMutation({
    mutationFn: ({ id, group }: { id: string; group: string | null }) => api.setFleetAgentGroup(id, group),
    onMutate: ({ id, group }) => optimisticAgent(id, { group }),
    onError: (e, _v, ctx) => onWriteError(e, ctx),
    onSettled: invalidateFleet,
  });
  const createGroupMut = useMutation({ mutationFn: (name: string) => api.createFleetGroup(name), onSettled: invalidateFleet });

  const togglePin = useCallback(
    (a: FleetAgent) => {
      pinMut.mutate({ id: a.id, pinned: !a.pinned }); // the optimistic patch + rollback live in the mutation
    },
    [pinMut],
  );
  const moveToGroup = useCallback(
    (a: FleetAgent, group: string | null) => {
      if (group) setCollapsedState(collapsed.filter((k) => k !== group)); // show where it lands
      moveMut.mutate({ id: a.id, group }); // the optimistic patch + rollback live in the mutation
    },
    [collapsed, setCollapsedState, moveMut],
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
      // The server owns the authoritative name check, so a name that clears the local rules can
      // still be rejected here — e.g. a duplicate created from another browser that the 3s poll
      // hasn't picked up yet (a 400). Return the hub's plain copy for a rejected NAME, else the
      // generic copy (`fleetWriteErrorMessage`) — instead of rejecting into the caller: a bare
      // reject would skip the error UI, leave the phone sheet stuck, and log an unhandled rejection.
      // Every caller renders the returned string. On success the mutation's invalidation reconciles.
      try {
        const res = await createGroupMut.mutateAsync(raw.trim());
        moveToGroup(a, res.group.id);
        return null;
      } catch (e) {
        return fleetWriteErrorMessage(e);
      }
    },
    [validateGroupName, createGroupMut, moveToGroup],
  );

  const doSwitch = useCallback(
    (a: FleetAgent) => {
      const target = slugOf(a);
      if (target === slug) return; // already here
      window.location.href = agentHref(target);
    },
    [slug],
  );

  // ── sections ───────────────────────────────────────────────────────────────────────────────
  // Each agent appears in Current, Pinned, or its home section, in that order.
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
    for (const a of agents) {
      if (a.id !== current?.id && !a.pinned) byKey.get(homeOf(a))?.agents.push(a);
    }
    return base;
  }, [agents, groups, homeOf, current]);

  const pinned = useMemo(
    () => agents
      .filter((a) => a.pinned && a.id !== current?.id)
      .sort((x, y) => (x.pinned_at ?? "").localeCompare(y.pinned_at ?? "")),
    [agents, current],
  );
  const matches = useCallback(
    (a: FleetAgent, groupName: string) =>
      [nameOf(a), a.name, groupName, a.remote ? a.machine ?? "" : ""].join(" ").toLowerCase().includes(query),
    [query],
  );
  const groupNameOf = (a: FleetAgent) => sections.find((s) => s.key === homeOf(a))?.name ?? "";
  const shownCurrent = current && (!filtering || matches(current, groupNameOf(current))) ? current : undefined;
  const shownPinned = pinned.filter((a) => !filtering || matches(a, groupNameOf(a)));
  // Enter follows the same order as the visible list.
  const topMatch = filtering
    ? shownCurrent ?? shownPinned[0] ?? sections.flatMap((s) => s.agents.filter((a) => matches(a, s.name)))[0]
    : null;
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
  const menuRef = useRef<MenuHandle>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => {
    if (!menuOpen || isMobile || !showFilter || typeof requestAnimationFrame !== "function") return;
    const id = requestAnimationFrame(() => {
      (document.querySelector(".pl-menu:not(.fleet-rowmenu) .pl-menu__filter input") as HTMLElement | null)?.focus();
    });
    return () => cancelAnimationFrame(id);
  }, [menuOpen, isMobile, showFilter]);

  // ── desktop header keyboard (the blocked-footer-items regression fix) ───────────────────────
  // Radix's menu blocks Tab and only roving-focuses MenuItems, so the two action buttons — plain
  // buttons by design, since they carry an aria-label, a title and the disabled-reason tooltip a
  // MenuItem can't — would be keyboard-unreachable. Make the header a toolbar: Left/Right move
  // across the filter and the two buttons, Down drops into the list, Up returns to the filter. The
  // filter↔list arrows (Down into the first row, Up back to it) stay owned by the DS MenuFilter.
  const headerRef = useRef<HTMLDivElement>(null);
  const headerStops = useCallback((): HTMLElement[] => {
    const root = headerRef.current;
    if (!root) return [];
    const input = root.querySelector<HTMLElement>(".pl-menu__filter input");
    const actions = [...root.querySelectorAll<HTMLElement>(".fleet-header-actions button:not([disabled])")];
    return [input, ...actions].filter((el): el is HTMLElement => el !== null);
  }, []);
  const firstMenuItem = useCallback(
    (): HTMLElement | null =>
      headerRef.current
        ?.closest("[data-radix-menu-content]")
        ?.querySelector<HTMLElement>(
          '[role="menuitem"]:not([data-disabled]),[role="menuitemradio"]:not([data-disabled]),[role="menuitemcheckbox"]:not([data-disabled])',
        ) ?? null,
    [],
  );
  const onHeaderKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      const el = e.target as HTMLElement;
      const stops = headerStops();
      const idx = stops.indexOf(el);
      if (idx < 0) return;
      const input = el instanceof HTMLInputElement ? el : null;
      // In the text box, Left/Right first move the caret; only hop controls from the far edge.
      const atEnd = !input || (input.selectionStart === input.value.length && input.selectionStart === input.selectionEnd);
      const atStart = !input || (input.selectionStart === 0 && input.selectionEnd === 0);
      if (e.key === "ArrowRight" && atEnd && stops[idx + 1]) {
        e.preventDefault();
        stops[idx + 1].focus();
      } else if (e.key === "ArrowLeft" && atStart && idx > 0) {
        e.preventDefault();
        stops[idx - 1].focus();
      } else if (e.key === "ArrowDown" && !input) {
        e.preventDefault();
        firstMenuItem()?.focus();
      } else if (e.key === "ArrowUp" && !input) {
        const filterInput = stops[0] instanceof HTMLInputElement ? stops[0] : null;
        if (filterInput) {
          e.preventDefault();
          filterInput.focus();
        }
      }
    },
    [headerStops, firstMenuItem],
  );
  // Below the filter threshold there is no MenuFilter to arrow up into — the DS registers its
  // Up-from-first-row→filter bridge only when a filter is mounted — so the header would be
  // unreachable when focus opens on the first row. Bridge Up-from-first-row to the first action
  // button instead. The listener sits on `document` in the capture phase so it runs before Radix's
  // roving-focus handler (a React-synthetic listener on the app root) gets to loop focus.
  useEffect(() => {
    if (!menuOpen || isMobile || showFilter || typeof document === "undefined") return;
    const onCapture = (ev: Event) => {
      const ke = ev as KeyboardEvent;
      if (ke.key !== "ArrowUp") return;
      const menu = document.querySelector(".pl-menu:not(.fleet-rowmenu)");
      if (!menu) return;
      const first = menu.querySelector(
        '[role="menuitem"]:not([data-disabled]),[role="menuitemradio"]:not([data-disabled]),[role="menuitemcheckbox"]:not([data-disabled])',
      );
      if (first && document.activeElement === first) {
        const btn = menu.querySelector<HTMLElement>(".fleet-header-actions button:not([disabled])");
        if (btn) {
          ke.preventDefault();
          ke.stopPropagation();
          btn.focus();
        }
      }
    };
    document.addEventListener("keydown", onCapture, { capture: true });
    return () => document.removeEventListener("keydown", onCapture, { capture: true });
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
  if (shownCurrent) desktopBody.push(agentRow(shownCurrent, "current"));
  if (shownPinned.length) {
    pushSep();
    desktopBody.push(<MenuLabel key="pinned-label">Pinned</MenuLabel>, ...shownPinned.map((a) => agentRow(a, "pinned")));
  }
  let matchCount = (shownCurrent ? 1 : 0) + shownPinned.length;
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
        current={shownCurrent}
        pinned={shownPinned}
        sections={sections}
        slug={slug}
        isCollapsed={isCollapsed}
        toggleCollapsed={toggleCollapsed}
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
        ref={menuRef}
        className="fleet-switcher-menu"
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
        <div className="fleet-switcher-header" ref={headerRef} onKeyDown={onHeaderKeyDown}>
          {showFilter ? (
            <MenuFilter
              value={filter}
              onValueChange={setFilter}
              onSubmit={onFilterSubmit}
              placeholder="Find an agent"
              aria-label="Find an agent by name or group"
            />
          ) : null}
          <HeaderButtons
            fleetSettingsBlocked={fleetSettingsBlocked}
            onNewAgent={() => {
              menuRef.current?.close();
              onNewAgent?.();
            }}
            onManageFleet={() => {
              menuRef.current?.close();
              onManageFleet?.();
            }}
          />
        </div>
        {desktopBody}
      </Menu>
      {canWrite ? (
        <RowMenu ref={rowMenu} groups={groups} togglePin={togglePin} moveToGroup={moveToGroup} createAndMove={createAndMove} />
      ) : null}
    </>
  );
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
              // On success close the menu (onOpenChange resets view/target), so re-opening
              // starts clean — otherwise the 'new' view lingers with the typed name and a
              // second Enter after the refetch shows a false "already a group" error.
              if (err) setNewErr(err);
              else ref.current?.close();
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
  sections: HomeSection[];
  slug: string;
  isCollapsed: (key: string) => boolean;
  toggleCollapsed: (key: string) => void;
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

  const { current, pinned, sections, slug, filtering, matches } = props;

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
  if (current) list.push(phoneRow(current, "current"));
  if (pinned.length) {
    list.push(
      <div className="fleet-slabel pl-menu__group-label" key="pinned-label">Pinned</div>,
      ...pinned.map((a) => phoneRow(a, "pinned")),
    );
  }
  let matchCount = (current ? 1 : 0) + pinned.length;
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

      <Drawer
        open={sheet === "switcher"}
        onClose={closeSheets}
        side="bottom"
        size="85dvh"
        title={
          <span className="fleet-sheet-title">
            <span>Switch agent</span>
            <HeaderButtons
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
          </span>
        }
        className="fleet-sheet"
      >
        <div className="fleet-sheet-inner">
          {/* On touch the header icons' disabled-reason tooltip is never seen, so spell it out. */}
          {props.fleetSettingsBlocked ? <p className="fleet-sheet-note fleet-act-note">{props.fleetSettingsBlocked}</p> : null}
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

function HeaderButtons({
  fleetSettingsBlocked,
  onNewAgent,
  onManageFleet,
}: {
  fleetSettingsBlocked: string | null;
  onNewAgent?: () => void;
  onManageFleet?: () => void;
}) {
  return (
    <span className="fleet-header-actions">
      {[
        { label: "New agent", Icon: Plus, onClick: onNewAgent },
        { label: "Fleet settings", Icon: Settings, onClick: onManageFleet },
      ].map(({ label, Icon, onClick }) => (
        <Tooltip key={label} label={fleetSettingsBlocked ?? label}>
          <button
            type="button"
            className="pl-iconbtn"
            aria-label={label}
            title={fleetSettingsBlocked ?? label}
            disabled={!!fleetSettingsBlocked}
            onClick={onClick}
          >
            <Icon size={16} aria-hidden />
          </button>
        </Tooltip>
      ))}
    </span>
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
