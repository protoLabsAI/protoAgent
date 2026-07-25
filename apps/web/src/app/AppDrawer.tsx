import type { ReactNode } from "react";
import { BookOpen, Bug, Github, ScrollText, Settings2 } from "lucide-react";

import { Drawer } from "@protolabsai/ui/overlays";

import "./app-drawer.css";

type SurfaceItem = { id: string; label: string; icon: ReactNode };

/**
 * The app menu drawer — a right-side sheet opened by the header hamburger. One drawer for
 * both modes: on desktop it holds the single Settings door + the Docs/Changelog/GitHub links;
 * on mobile it ALSO lists the surfaces (it's the mobile "more").
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
  identity,
}: {
  open: boolean;
  onClose: () => void;
  mobile: boolean;
  surfaces: SurfaceItem[];
  activeSurface: string;
  onSelectSurface: (id: string) => void;
  onOpenGlobal: (section?: string) => void;
  version?: string;
  /** Agent/fleet identity. On mobile the chat-first shell's header carries the SESSION
   *  title instead of the DS Header, so the fleet switcher lives here — otherwise a fleet
   *  operator loses any indication of which agent they're talking to. */
  identity?: ReactNode;
}) {
  // Each action closes the drawer after running.
  const act = (fn: () => void) => () => {
    fn();
    onClose();
  };

  // DS Drawer (#2222) — the hand-rolled sheet asserted aria-modal without keeping the
  // contract (no focus trap, no scroll-lock, mounted inside the header subtree). The DS
  // Drawer owns all of that plus Esc/backdrop dismiss and the <body> portal (#463), so
  // this component is just the menu content now. The e2e hook (data-testid) rides the
  // body wrapper — every spec targets body content through it, never the chrome.
  return (
    <Drawer open={open} onClose={onClose} side="right" title="Menu" width="min(320px, 88vw)"
      footer={
        <div className="app-drawer-foot">
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
        </div>
      }
    >
      <div className="app-drawer-body" data-testid="app-drawer">
          {mobile && identity ? <div className="app-drawer-identity">{identity}</div> : null}
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
            {/* One Settings door (ADR 0048 §2.4) — Telemetry is a section inside it (Box group),
                reachable via the sidenav or a ⌘K deep-link, not a second drawer shortcut. */}
            <button type="button" className="app-drawer-item" onClick={act(() => onOpenGlobal())}>
              <span className="app-drawer-ico"><Settings2 size={16} /></span>
              Settings
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
    </Drawer>
  );
}
