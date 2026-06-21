// The fixed core console surfaces (ADR 0035 S3). Shared so the main shell AND the
// desktop quick-launcher (ADR 0057) build their command lists from the SAME source —
// add a core surface here and it shows up in both the rail and ⌘K / the launcher.
import type { ReactNode } from "react";
import { BookMarked, LayoutDashboard, MessageSquare } from "lucide-react";

export type CoreSurface = { id: string; label: string; icon: ReactNode };

// Keep in lock-step with App's rail: Chat is excluded from rail placement there (it's
// pinned + always mounted), but is a valid palette/launcher "go to" target.
export const CORE_SURFACES: CoreSurface[] = [
  { id: "chat", label: "Chat", icon: <MessageSquare size={18} /> },
  // The Work hub (2026-06) folds the former Tasks / Goals / Schedule rail surfaces into one
  // right-rail surface (Overview + Goals/Tasks/Schedule tabs). Activity is a utility-bar
  // widget; "agent" folded into Settings ▸ Workspace; "plugins" into Settings ▸ Plugins.
  { id: "work", label: "Work", icon: <LayoutDashboard size={18} /> },
  { id: "knowledge", label: "Knowledge", icon: <BookMarked size={18} /> },
  // Settings moved off the rail into a utility-bar pill (2026-06 consolidation) → the
  // settings dialog. It's still a valid ⌘K "go to" via the global deep-links.
];
