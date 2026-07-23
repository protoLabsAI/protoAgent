import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, Plus, Settings } from "lucide-react";
import type { ReactNode } from "react";

import { Menu, MenuItem, MenuSeparator } from "@protolabsai/ui/menu";
import { Tooltip } from "@protolabsai/ui/overlays";
import { Badge } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";

import { agentHref, api, currentSlug } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { fleetSettingsDisabledReason } from "./fleetSettingsGate";

// The URL slug is the agent's STABLE id, never its (editable) display name — renaming an agent
// must not change its URL/bookmarks. `host` is the reserved slug for this instance.
const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);

// Topbar agent switcher (ADR 0042 slug routing). The focused agent lives in the URL
// (/app/agent/<slug>/), so picking one NAVIGATES there — each window is its own agent, a reload
// can't desync. The dropdown ALWAYS shows (so New agent + Fleet settings are reachable even with a
// single agent); only a hard fleet-API error falls back to the plain name. The dropdown is the DS
// Menu (#1078): open/close, outside-click, and focus trap all come from @protolabsai/ui.
export function FleetSwitcher({
  fallbackName,
  onNewAgent,
  onManageFleet,
}: {
  fallbackName: ReactNode;
  onNewAgent?: () => void;
  onManageFleet?: () => void;
}) {
  // Poll so the topbar reflects the fleet live (a newly-added agent shows up in the list).
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false, refetchInterval: 3_000 });

  const agents = fleet.data?.agents ?? [];
  const slug = currentSlug(); // the agent THIS window is on
  const current = agents.find((a) => slugOf(a) === slug);
  // Fleet settings are hub-only (#1708): non-null in a member window (a hub slug window,
  // or a spawned workspace member reached directly) — the item disables with this tooltip.
  const fleetSettingsBlocked = fleetSettingsDisabledReason(agents, slug);

  // Only a hard fleet-API error hides the switcher; otherwise it's always available.
  if (fleet.isError) return <>{fallbackName}</>;

  return (
    <Menu
      trigger={
        <button type="button" className="fleet-switcher-trigger" data-testid="fleet-switcher" aria-label="Switch agent">
          <span>{current?.name ?? fallbackName}</span>
          <ChevronDown size={14} />
        </button>
      }
    >
      {agents.map((a) => {
        const isCurrent = slugOf(a) === slug;
        return (
          <MenuItem
            key={a.name}
            icon={<StatusDot status={a.running ? "success" : "neutral"} pulse={a.running} />}
            onSelect={() => {
              if (!isCurrent) window.location.href = agentHref(slugOf(a)); // navigate → this agent
            }}
          >
            <span className="fleet-switcher-name">
              {a.name}
              {a.host ? <Badge status="neutral">this instance</Badge> : null}
              {isCurrent ? <Check size={14} className="fleet-switcher-check" /> : null}
            </span>
          </MenuItem>
        );
      })}
      {agents.length > 0 ? <MenuSeparator /> : null}
      {fleetSettingsBlocked ? (
        // Adding a member is a host-only op, same as Fleet settings (a member window's
        // /api/fleet is a fleet-of-one — "New agent" there would spawn a nested fleet by
        // accident, #1999). Disabled + tooltip, not hidden, so it stays discoverable.
        <Tooltip label={fleetSettingsBlocked} side="left">
          <MenuItem icon={<Plus size={14} />} disabled>
            New agent
          </MenuItem>
        </Tooltip>
      ) : (
        <MenuItem icon={<Plus size={14} />} onSelect={() => onNewAgent?.()}>
          New agent
        </MenuItem>
      )}
      {fleetSettingsBlocked ? (
        // Disabled, not hidden — discoverable: the tooltip says WHERE fleet settings live.
        // The DS Tooltip's own wrapper span is the hover target (a disabled Radix menu item
        // is pointer-events:none, so the item itself can't fire the tooltip).
        <Tooltip label={fleetSettingsBlocked} side="left">
          <MenuItem icon={<Settings size={14} />} disabled>
            Fleet settings
          </MenuItem>
        </Tooltip>
      ) : (
        <MenuItem icon={<Settings size={14} />} onSelect={() => onManageFleet?.()}>
          Fleet settings
        </MenuItem>
      )}
    </Menu>
  );
}
