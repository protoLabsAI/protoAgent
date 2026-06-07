import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

// The canonical sub-tab strip. Every surface with sub-tabs renders it through this
// one component, ALWAYS above the panel card — so Settings, Knowledge, Activity,
// System, and plugin views all share the same layout (single source of truth, ADR 0020).
// Previously some surfaces rendered the strip inside the panel card (Settings/PluginView)
// and some above it (App-level rail surfaces); this unifies them.

export type StageTab = {
  id: string;
  label: string;
  icon?: LucideIcon;
  badge?: ReactNode;   // e.g. an unread count
  testId?: string;
};

export function StageSubnav({
  tabs,
  active,
  onSelect,
}: {
  tabs: StageTab[];
  active: string;
  onSelect: (id: string) => void;
}) {
  if (!tabs.length) return null;
  return (
    <div className="stage-subnav">
      {tabs.map((t) => {
        const Icon = t.icon;
        return (
          <button
            key={t.id}
            type="button"
            className={t.id === active ? "active" : ""}
            onClick={() => onSelect(t.id)}
            data-testid={t.testId}
          >
            {Icon ? <Icon size={15} /> : null}
            {t.label}
            {t.badge}
          </button>
        );
      })}
    </div>
  );
}
