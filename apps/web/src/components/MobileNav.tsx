import { useState } from "react";
import { Menu, X } from "lucide-react";
import type { ReactNode } from "react";

export type MobileItem = { id: string; label: string; icon: ReactNode };

// Mobile bottom quick-bar + hamburger drawer (ADR 0035 S4). Dumb + props-driven (extraction-ready).
// The drawer is a minimal interim sheet — swap for @protolabsai/ui's Drawer when it lands (ADR 0037 D7).
export function MobileNav({
  items,
  activeId,
  onSelect,
  quickBarIds,
}: {
  items: MobileItem[];
  activeId: string;
  onSelect: (id: string) => void;
  quickBarIds: string[];
}) {
  const [open, setOpen] = useState(false);
  const byId = new globalThis.Map(items.map((i) => [i.id, i] as const));
  const quick = quickBarIds.map((id) => byId.get(id)).filter((i): i is MobileItem => Boolean(i)).slice(0, 5);
  const pick = (id: string) => { onSelect(id); setOpen(false); };

  return (
    <>
      <nav className="mobile-bar" aria-label="Quick surfaces">
        {quick.map((it) => (
          <button key={it.id} className={`mobile-tab${it.id === activeId ? " active" : ""}`} type="button" onClick={() => pick(it.id)}>
            {it.icon}
            <span>{it.label}</span>
          </button>
        ))}
        <button className="mobile-tab" type="button" onClick={() => setOpen(true)} aria-label="All surfaces">
          <Menu size={18} />
          <span>More</span>
        </button>
      </nav>
      {open ? (
        <div className="mobile-drawer-overlay" onClick={() => setOpen(false)}>
          <div className="mobile-drawer" role="dialog" aria-label="Surfaces" onClick={(e) => e.stopPropagation()}>
            <div className="mobile-drawer-head">
              <span>Surfaces</span>
              <button type="button" onClick={() => setOpen(false)} aria-label="Close"><X size={18} /></button>
            </div>
            <div className="mobile-drawer-list">
              {items.map((it) => (
                <button key={it.id} className={it.id === activeId ? "active" : ""} type="button" onClick={() => pick(it.id)}>
                  {it.icon}
                  <span>{it.label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
