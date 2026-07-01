import type { ReactNode } from "react";

import { PanelHeader } from "@protolabsai/ui/navigation";

import { StagePanel } from "../app/ErrorBoundary";

// Shared chrome for a bespoke Settings sub-panel (#1545). Wraps content in the canonical
// StagePanel scaffold (ADR 0013 — ErrorBoundary + Suspense) plus the DS PanelHeader title bar
// and the scrolling `stage-body`, so hand-built panels (Keyboard, Delegates) match the
// schema-driven ones (SettingsCategoryPanel) and the other bespoke panels (Theme, Chat).
// One container → the header/padding/scroll treatment can't drift per panel.
export function SettingsSubPanel({
  label,
  title,
  kicker,
  actions,
  children,
}: {
  /** Error/loading label for the StagePanel scaffold. */
  label: string;
  title: ReactNode;
  kicker?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <StagePanel label={label}>
      <PanelHeader title={title} kicker={kicker} actions={actions} />
      <div className="stage-body">{children}</div>
    </StagePanel>
  );
}
