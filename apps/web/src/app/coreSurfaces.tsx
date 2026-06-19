// The fixed core console surfaces (ADR 0035 S3). Shared so the main shell AND the
// desktop quick-launcher (ADR 0057) build their command lists from the SAME source —
// add a core surface here and it shows up in both the rail and ⌘K / the launcher.
import type { ReactNode } from "react";
import { BookMarked, Boxes, CalendarClock, MessageSquare, Puzzle, Settings2, Target } from "lucide-react";

export type CoreSurface = { id: string; label: string; icon: ReactNode };

// Keep in lock-step with App's rail: Chat is excluded from rail placement there (it's
// pinned + always mounted), but is a valid palette/launcher "go to" target.
export const CORE_SURFACES: CoreSurface[] = [
  { id: "chat", label: "Chat", icon: <MessageSquare size={18} /> },
  // Activity left the rail in the 2026-06 IA pass — it's a read-only utility-bar widget
  // now (ActivityWidget, bottom-left widgets cluster).
  { id: "schedule", label: "Schedule", icon: <CalendarClock size={18} /> },
  // "studio" (Workflows) is contributed via src/ext/workflows.tsx, gated on the
  // workflows plugin (lean core) — no longer a hardcoded core surface.
  { id: "knowledge", label: "Knowledge", icon: <BookMarked size={18} /> },
  // "agent" folded into Settings ▸ Workspace (ADR 0048 S-C) — no longer a rail surface.
  { id: "plugins", label: "Plugins", icon: <Puzzle size={18} /> },
  { id: "settings", label: "Settings", icon: <Settings2 size={18} /> },
  { id: "beads", label: "Beads", icon: <Boxes size={18} /> },
  { id: "goals", label: "Goals", icon: <Target size={18} /> },
];
