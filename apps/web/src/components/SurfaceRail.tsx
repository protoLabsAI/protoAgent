import type { MouseEvent, ReactNode } from "react";

export type RailItem = { id: string; label: string; icon: ReactNode; badge?: number; dot?: boolean };

// A vertical rail of surface icons (ADR 0035/0036). Dumb + fully props-driven — both the left and
// right rails render through it, and it's extraction-ready for @protolabsai/ui's AppShell (no
// protoAgent-specific coupling; theming is class/token-only).
export function SurfaceRail({
  side,
  ariaLabel,
  items,
  activeId,
  onSelect,
  onContextMenu,
}: {
  side: "left" | "right";
  ariaLabel: string;
  items: RailItem[];
  activeId: string;
  onSelect: (id: string) => void;
  onContextMenu: (e: MouseEvent, id: string) => void;
}) {
  return (
    <aside className={`rail${side === "right" ? " rail-right" : ""}`} aria-label={ariaLabel}>
      {items.map((it) => (
        <RailButton
          key={it.id}
          active={it.id === activeId}
          label={it.label}
          icon={it.icon}
          badge={it.badge}
          dot={it.dot}
          onClick={() => onSelect(it.id)}
          onContextMenu={(e) => onContextMenu(e, it.id)}
        />
      ))}
    </aside>
  );
}

function RailButton({
  active,
  label,
  icon,
  onClick,
  onContextMenu,
  badge,
  dot,
}: {
  active: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  onContextMenu?: (e: MouseEvent) => void;
  badge?: number;
  // A small pulsing indicator (no count) — e.g. a chat turn streaming in the background.
  dot?: boolean;
}) {
  return (
    <button className={active ? "active" : ""} type="button" onClick={onClick} onContextMenu={onContextMenu} title={label} aria-label={label}>
      {icon}
      <span>{label}</span>
      {badge ? (
        <span className="rail-badge" data-testid="activity-badge">{badge > 9 ? "9+" : badge}</span>
      ) : dot ? (
        <span className="rail-dot" data-testid="chat-streaming-dot" aria-label="streaming" />
      ) : null}
    </button>
  );
}
