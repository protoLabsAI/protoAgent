import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ExternalLink, Plus } from "lucide-react";
import type { ReactNode } from "react";

import { Menu, MenuItem, MenuSeparator } from "@protolabsai/ui/menu";
import { Badge } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";

import { agentHref, api, currentSlug } from "../lib/api";
import { queryKeys } from "../lib/queries";

// The URL slug is the agent's STABLE id, never its (editable) display name — renaming an agent
// must not change its URL/bookmarks. `host` is the reserved slug for this instance.
const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);

// Topbar agent switcher (ADR 0042 slug routing). The focused agent lives in the URL
// (/app/agent/<slug>/), so picking one NAVIGATES there — each window is its own agent, a reload
// can't desync, and you can open a second agent in a new window. Single-agent (just the host) →
// plain name, no dropdown. The dropdown is the DS Menu (#1078): open/close, outside-click, focus
// trap, and the trailing per-row "open in a new window" action all come from @protolabsai/ui.
export function FleetSwitcher({ fallbackName, onNewAgent }: { fallbackName: ReactNode; onNewAgent?: () => void }) {
  // Poll so the topbar reflects the fleet live (a newly-added agent makes the switcher appear).
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false, refetchInterval: 3_000 });

  const agents = fleet.data?.agents ?? [];
  const slug = currentSlug(); // the agent THIS window is on
  const current = agents.find((a) => slugOf(a) === slug);

  // Solo (just the host) or no hub → plain name, no switcher.
  if (fleet.isError || agents.length <= 1) return <>{fallbackName}</>;

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
      <MenuSeparator />
      <MenuItem icon={<Plus size={14} />} onSelect={() => onNewAgent?.()}>
        New agent
      </MenuItem>
    </Menu>
  );
}
