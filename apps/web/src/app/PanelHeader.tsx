import type { ReactNode } from "react";

// The canonical panel header — title + optional kicker on the left, optional actions
// on the right. Every surface renders its header through this one component so titles,
// kickers, and action-button placement are identical across panels (single source of
// truth). `compact` is for nested/secondary panels (Inbox, Goals, Beads, Notes): a
// smaller h2 + tighter padding (the .compact modifier).

export function PanelHeader({
  title,
  kicker,
  actions,
  compact = false,
}: {
  title: ReactNode;
  kicker?: ReactNode;
  actions?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "panel-header compact" : "panel-header"}>
      <div>
        {compact ? <h2>{title}</h2> : <h1>{title}</h1>}
        {kicker != null && kicker !== "" ? <p className="panel-kicker">{kicker}</p> : null}
      </div>
      {actions != null ? <div className="panel-actions">{actions}</div> : null}
    </div>
  );
}
