import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, Plus } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { api, setActivePrefix } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { setLayoutAgent, useUI } from "../state/uiStore";

// Topbar agent switcher (ADR 0042). The agent name becomes a dropdown of the fleet;
// picking one POSTs /activate and flips the console's API base to /active/* so the whole
// console reads/writes the focused agent (the in-place switch). In single-agent mode (no
// hub → /api/fleet errors) it falls back to the plain name with no dropdown.
export function FleetSwitcher({ fallbackName, onNewAgent }: { fallbackName: ReactNode; onNewAgent?: () => void }) {
  const qc = useQueryClient();
  // Own observer: no poll (the Agents panel polls); retry off so single-agent fails fast to the fallback.
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false, staleTime: 5_000 });
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
  const active = fleet.data?.active ?? null;

  // Each agent keeps its own layout (rail order/widths/plugins). Namespace the persisted
  // key by the focused agent; on a real *switch* reload from the new agent's key. The first
  // observation only syncs the key — the store already hydrated from it at module load
  // (setLayoutAgent reads localStorage synchronously), so rehydrating there would reset it.
  const prevAgent = useRef<string | null>(null);
  useEffect(() => {
    const a = active ?? "";
    if (prevAgent.current === null) {
      prevAgent.current = a;
      setLayoutAgent(a);
      return;
    }
    if (prevAgent.current !== a) {
      prevAgent.current = a;
      setLayoutAgent(a);
      void useUI.persist.rehydrate(); // a switch → load the new agent's saved layout
    }
  }, [active]);

  const host = agents.find((a) => a.host);
  const isActive = (a: { name: string; host?: boolean }) => a.name === active || (!!a.host && active === null);

  const activate = useMutation({
    mutationFn: (a: { name: string; host?: boolean }) => api.activateAgent(a.name),
    onSuccess: (_res, a) => {
      // Host → talk to /api directly (proxy cleared); peer → route through /active.
      setActivePrefix(a.host ? "" : "/active");
      setOpen(false);
      qc.invalidateQueries();
    },
  });

  // Solo (just the host) or no hub → plain name, no switcher. It only appears once there's
  // a real fleet (host + at least one peer).
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
        <span>{active ?? host?.name ?? fallbackName}</span>
        <ChevronDown size={14} />
      </button>
      {open ? (
        <div className="fleet-switcher-menu" role="menu">
          {agents.map((a) => (
            <button
              key={a.name}
              type="button"
              role="menuitem"
              className={`fleet-switcher-item${isActive(a) ? " active" : ""}`}
              disabled={activate.isPending}
              onClick={() => activate.mutate(a)}
            >
              <span className={`fleet-dot ${a.running ? "running" : "stopped"}`} aria-hidden />
              <span className="fleet-switcher-name">
                {a.name}
                {a.host ? <span className="fleet-host-tag">this instance</span> : null}
              </span>
              {isActive(a) ? <Check size={14} /> : null}
            </button>
          ))}
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
