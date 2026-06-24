import { useEffect } from "react";
import type { ReactNode } from "react";
import { BarChart3, BookOpen, Bug, Github, ScrollText, Settings2, X } from "lucide-react";

import { Button } from "@protolabsai/ui/primitives";

import "./app-drawer.css";

type SurfaceItem = { id: string; label: string; icon: ReactNode };

/**
 * The app menu drawer — a right-side sheet opened by the header hamburger (2026-06-18
 * IA pass). One drawer for both modes: on desktop it holds the box-level/global actions
 * (Global settings, Telemetry) + the Docs/Changelog/GitHub links; on mobile it ALSO lists the
 * surfaces (it's the mobile "more"). Workspace settings stay in the rail surface, not here.
 */
export function AppDrawer({
  open,
  onClose,
  mobile,
  surfaces,
  activeSurface,
  onSelectSurface,
  onOpenGlobal,
  version,
}: {
  open: boolean;
  onClose: () => void;
  mobile: boolean;
  surfaces: SurfaceItem[];
  activeSurface: string;
  onSelectSurface: (id: string) => void;
  onOpenGlobal: (section?: string) => void;
  version?: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  // Each action closes the drawer after running.
  const act = (fn: () => void) => () => {
    fn();
    onClose();
  };

  return (
    <div className="app-drawer-root" role="dialog" aria-modal="true" aria-label="Menu">
      <div className="app-drawer-backdrop" onClick={onClose} />
      <aside className="app-drawer" data-testid="app-drawer">
        <header className="app-drawer-head">
          <strong>Menu</strong>
          <Button icon variant="ghost" type="button" aria-label="Close menu" onClick={onClose}>
            <X size={16} />
          </Button>
        </header>
        <div className="app-drawer-body">
          {mobile && surfaces.length ? (
            <section className="app-drawer-group">
              <p className="app-drawer-label">Go to</p>
              {surfaces.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`app-drawer-item${s.id === activeSurface ? " on" : ""}`}
                  onClick={act(() => onSelectSurface(s.id))}
                >
                  <span className="app-drawer-ico">{s.icon}</span>
                  {s.label}
                </button>
              ))}
            </section>
          ) : null}

          <section className="app-drawer-group">
            <p className="app-drawer-label">Settings</p>
            <button type="button" className="app-drawer-item" onClick={act(() => onOpenGlobal())}>
              <span className="app-drawer-ico"><Settings2 size={16} /></span>
              Settings
            </button>
            <button type="button" className="app-drawer-item" onClick={act(() => onOpenGlobal("telemetry"))}>
              <span className="app-drawer-ico"><BarChart3 size={16} /></span>
              Telemetry
            </button>
          </section>

          <section className="app-drawer-group">
            <p className="app-drawer-label">Links</p>
            <a
              className="app-drawer-item"
              href="https://protolabsai.github.io/protoAgent/"
              target="_blank"
              rel="noreferrer"
              onClick={onClose}
            >
              <span className="app-drawer-ico"><BookOpen size={16} /></span>
              Docs
            </a>
            <a
              className="app-drawer-item"
              href="https://agent.protolabs.studio/changelog/"
              target="_blank"
              rel="noreferrer"
              onClick={onClose}
            >
              <span className="app-drawer-ico"><ScrollText size={16} /></span>
              Changelog
            </a>
            <a
              className="app-drawer-item"
              href="https://github.com/protoLabsAI/protoAgent"
              target="_blank"
              rel="noreferrer"
              onClick={onClose}
            >
              <span className="app-drawer-ico"><Github size={16} /></span>
              GitHub
            </a>
            <a
              className="app-drawer-item"
              href="https://github.com/protoLabsAI/protoAgent/issues/new/choose"
              target="_blank"
              rel="noreferrer"
              onClick={onClose}
            >
              <span className="app-drawer-ico"><Bug size={16} /></span>
              Report a bug
            </a>
          </section>
        </div>
        <footer className="app-drawer-foot">
          {version ? <span className="app-drawer-version">v{version}</span> : null}
          {/* P4: the wordmark is sacred — protoLabs.studio, exactly. */}
          <a
            className="app-drawer-built"
            href="https://protolabs.studio"
            target="_blank"
            rel="noreferrer"
            onClick={onClose}
          >
            built by <strong>protoLabs.studio</strong>
          </a>
        </footer>
      </aside>
    </div>
  );
}
