import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ExternalLink, Plus, Settings } from "lucide-react";
import type { ReactNode } from "react";

import { Menu, MenuItem, MenuSeparator } from "@protolabsai/ui/menu";
import { Tooltip } from "@protolabsai/ui/overlays";
import { Badge } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";

import { agentHref, api, currentSlug } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { fleetDisabledReason } from "./fleetGate";

// The URL slug is the agent's STABLE id, never its (editable) display name — renaming an agent
// must not change its URL/bookmarks. `host` is the reserved slug for this instance.
const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);

// Topbar agent switcher (ADR 0042 slug routing). The focused agent lives in the URL
// (/app/agent/<slug>/), so picking one NAVIGATES there — each window is its own agent, a reload
// can't desync, and you can open a second agent in a new window. The dropdown ALWAYS shows (so
// New agent + Fleet settings are reachable even with a single agent); only a hard fleet-API error
// falls back to the plain name. The dropdown is the DS Menu (#1078): open/close, outside-click,
// focus trap, and the trailing per-row "open in a new window" action all come from @protolabsai/ui.
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
  // One gate for BOTH fleet items (#1999). They share a destination (Global ▸ Fleet), so
  // gating only "Fleet settings" left "+ New agent" as a live link to the same place —
  // which silently landed on an unrelated section. Non-null only when this instance is
  // itself a spawned workspace member being driven directly (see fleetGate.ts).
  const fleetBlocked = fleetDisabledReason(agents, slug);

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
            // Non-current rows get a trailing "open in a new window" action (two agents at once —
            // the point of slug routing); it doesn't trigger the row's navigate or close the menu.
            action={
              isCurrent
                ? undefined
                : {
                    icon: <ExternalLink size={13} />,
                    label: "Open in a new window",
                    onClick: () => window.open(agentHref(slugOf(a)), "_blank", "noopener"),
                  }
            }
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
      {/* Both fleet items share `fleetBlocked` — see fleetGate.ts for why that's one gate. */}
      <FleetMenuItem icon={<Plus size={14} />} blocked={fleetBlocked} onSelect={() => onNewAgent?.()}>
        New agent
      </FleetMenuItem>
      <FleetMenuItem icon={<Settings size={14} />} blocked={fleetBlocked} onSelect={() => onManageFleet?.()}>
        Fleet settings
      </FleetMenuItem>
    </Menu>
  );
}

/** A fleet menu item that disables (never hides) when the window can't manage the fleet —
 *  discoverable: the tooltip says WHERE the fleet lives. The DS Tooltip's own wrapper span
 *  is the hover target, because a disabled Radix menu item is pointer-events:none and so
 *  can't fire the tooltip itself. */
function FleetMenuItem({
  icon,
  blocked,
  onSelect,
  children,
}: {
  icon: ReactNode;
  blocked: string | null;
  onSelect: () => void;
  children: ReactNode;
}) {
  if (!blocked) {
    return (
      <MenuItem icon={icon} onSelect={onSelect}>
        {children}
      </MenuItem>
    );
  }
  return (
    <Tooltip label={blocked} side="left">
      <MenuItem icon={icon} disabled>
        {children}
      </MenuItem>
    </Tooltip>
  );
}
