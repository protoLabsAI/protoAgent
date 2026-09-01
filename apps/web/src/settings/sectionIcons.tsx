import { Activity, Bot, BookMarked, Brain, ChartColumn, Cpu, Database, FlaskConical, Gauge, Keyboard, KeyRound, Lock, MessageSquare, Network, Package, Palette, Plug, Puzzle, Server, Share2, Smartphone, Sparkles, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { SettingsSectionIcon } from "./sections";

// The section table's icon NAMES resolved to glyphs — the third piece of the ./sections split
// (#3285), in its own module rather than inside SettingsSurface.tsx so a consumer that needs a
// section's glyph but not its panels (⌘K, the desktop Launcher) can have it without the tree.
//
// STATIC named imports, deliberately, and NOT lib/lucideIcon's by-name resolver. That one is
// `lazy(() => import("lucide-react"))` reading the full `icons` map, which in this build is a
// 738 KB chunk nothing else eagerly loads, plus a Suspense tick per glyph. Paying that is right
// where the name is chosen at RUNTIME and nobody can enumerate the set — a fleet archetype, a
// plugin's icon (NewAgentPanel, SetupWizard). It is wrong here: these 23 names are a closed set
// known at build time, tree-shaken into the main chunk for the Settings rail already, so the
// second consumer costs no bytes at all. It also means the palette paints its glyphs on the
// first frame instead of flashing 22 identical Package boxes while the barrel downloads — and
// keeps the barrel off the Launcher window, which is most of what #3285 bought.
//
// `Record<SettingsSectionIcon, …>` makes the map exhaustive: a section whose icon name has no
// component here is a type error, not a missing glyph.
const ICONS: Record<SettingsSectionIcon, LucideIcon> = {
  Sparkles,
  KeyRound,
  Smartphone,
  Cpu,
  Brain,
  Database,
  Activity,
  Lock,
  Share2,
  Puzzle,
  Package,
  Wrench,
  Plug,
  BookMarked,
  Bot,
  Network,
  Gauge,
  Server,
  ChartColumn,
  Palette,
  MessageSquare,
  Keyboard,
  FlaskConical,
};

/** A section's glyph, ready to render. `size` defaults to 18 — what every palette row's icon
 *  renders at (the plugin-view rows, the chat row); the Settings rail asks for its own 15, a
 *  denser list. Takes the table's `icon` literal, so a name the map doesn't cover can't
 *  reach it. */
export function sectionIcon(name: SettingsSectionIcon, size = 18): ReactNode {
  const Icon = ICONS[name];
  return <Icon size={size} />;
}
