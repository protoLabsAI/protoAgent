import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ExternalLink, Plus } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Badge } from "@protolabsai/ui/primitives";

import { agentHref, api, currentSlug } from "../lib/api";
import { queryKeys } from "../lib/queries";

// The URL slug is the agent's STABLE id, never its (editable) display name — renaming an agent
// must not change its URL/bookmarks. `host` is the reserved slug for this instance.
const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);

// Topbar agent switcher (ADR 0042 slug routing). The focused agent lives in the URL
// (/app/agent/<slug>/), so picking one NAVIGATES there — each window is its own agent, a reload
// can't desync, and you can open a second agent in a new window. Single-agent (just the host) →
// plain name, no dropdown.
export function FleetSwitcher({ fallbackName, onNewAgent }: { fallbackName: ReactNode; onNewAgent?: () => void }) {
  // Poll so the topbar reflects the fleet live (a newly-added agent makes the switcher appear).
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false, refetchInterval: 3_000 });
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const agents = fleet.data?.agents ?? [];
  const slug = currentSlug(); // the agent THIS window is on
  const current = agents.find((a) => slugOf(a) === slug);

  // Solo (just the host) or no hub → plain name, no switcher.
  if (fleet.isError || agents.length <= 1) return <>{fallbackName}</>;

  return (
    <div className="fleet-switcher" ref={ref}>
      <button
        type="button"
        className="fleet-switcher-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="fleet-switcher"
      >
        <span>{current?.name ?? fallbackName}</span>
        <ChevronDown size={14} />
      </button>
      {open ? (
        <div className="fleet-switcher-menu" role="menu">
          {agents.map((a) => {
            const isCurrent = slugOf(a) === slug;
            return (
              <button
                key={a.name}
                type="button"
                role="menuitem"
                className={`fleet-switcher-item${isCurrent ? " active" : ""}`}
                onClick={() => {
                  if (!isCurrent) window.location.href = agentHref(slugOf(a)); // navigate → this agent
                }}
              >
                <span className={`fleet-dot ${a.running ? "running" : "stopped"}`} aria-hidden />
                <span className="fleet-switcher-name">
                  {a.name}
                  {a.host ? <Badge status="neutral">this instance</Badge> : null}
                </span>
                {isCurrent ? (
                  <Check size={14} />
                ) : (
                  // Open this agent in a second window (two agents at once — the whole point of
                  // slug routing). A span, not a nested button, to keep the markup valid.
                  <span
                    className="fleet-switcher-newwin"
                    role="button"
                    tabIndex={0}
                    title="Open in a new window"
                    onClick={(e) => {
                      e.stopPropagation();
                      window.open(agentHref(slugOf(a)), "_blank", "noopener");
                    }}
                  >
                    <ExternalLink size={13} />
                  </span>
                )}
              </button>
            );
          })}
          <div className="fleet-switcher-sep" />
          <button
            type="button"
            role="menuitem"
            className="fleet-switcher-item"
            onClick={() => {
              setOpen(false);
              onNewAgent?.();
            }}
          >
            <Plus size={14} /> New agent
          </button>
        </div>
      ) : null}
    </div>
  );
}
