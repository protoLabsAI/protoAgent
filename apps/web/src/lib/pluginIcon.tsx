import {
  Activity,
  BarChart3,
  BookMarked,
  BookOpen,
  Bot,
  Boxes,
  Brain,
  CalendarClock,
  Code,
  Coins,
  Compass,
  Cpu,
  Database,
  DollarSign,
  FileText,
  Folder,
  Gauge,
  GitBranch,
  Globe,
  Inbox,
  Layers,
  LayoutDashboard,
  LineChart,
  Map,
  MessageSquare,
  Network,
  Package,
  PieChart,
  Plug,
  Puzzle,
  Radar,
  Rocket,
  Satellite,
  Settings2,
  Shield,
  Ship,
  Sparkles,
  Table,
  Target,
  Terminal,
  TrendingUp,
  Wallet,
  Workflow,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { lazy, Suspense } from "react";
import type { ComponentType, LazyExoticComponent, ReactNode } from "react";

// Resolving a plugin-named icon to a glyph. Shared because a plugin names icons in two
// places and neither can ship markup across the sandbox: its rail/util-widget icon in the
// manifest (ADR 0026, resolved by App), and its context-menu item icons over the
// postMessage bridge (#3030, resolved by PluginView). Both take a NAME and resolve it here
// against the console's own icon set — plugin-supplied SVG never crosses the boundary.
//
// A plugin view names its rail glyph by lucide icon name. The curated set below is the
// common-case fast path (already bundled); anything else falls back to the full lucide set
// by name, so a plugin author can use ANY lucide icon — PascalCase (`LineChart`) or
// kebab-case (`line-chart`) — without us extending an allowlist. Unknown/missing → a
// generic plugin glyph.
const PLUGIN_VIEW_ICONS: Record<string, LucideIcon> = {
  // general
  Sparkles, LayoutDashboard, Puzzle, Boxes, Gauge, Target, Activity, Settings2,
  // data / viz
  BarChart3, LineChart, PieChart, TrendingUp, Database, Table, Layers,
  // comms / content
  MessageSquare, Inbox, CalendarClock, FileText, Folder, BookOpen, BookMarked,
  // dev / tools
  Code, Terminal, GitBranch, Package, Plug, Workflow, Network, Cpu, Zap,
  // ai
  Bot, Brain,
  // finance
  DollarSign, Coins, Wallet,
  // space / fleet / geo
  Rocket, Ship, Satellite, Radar, Globe, Compass, Map,
  // security
  Shield,
};

// "line-chart" / "line_chart" / "LineChart" → "LineChart" (lucide's key style).
function toPascalCase(name: string): string {
  return name.replace(/(^|[-_ ])([a-z0-9])/g, (_m, _sep, ch: string) => ch.toUpperCase());
}

// Off the curated path, resolve ANY lucide icon by name — but lazily: the dynamic
// import pulls the full lucide set into a separate chunk that only loads when a
// plugin actually uses a non-curated glyph, so the main bundle stays lean.
type IconComp = LazyExoticComponent<ComponentType<{ size?: number }>>;
// NB: `Map` is shadowed by the lucide Map icon import — use the global explicitly.
const lazyIconCache = new globalThis.Map<string, IconComp>();
function lazyLucideIcon(key: string): IconComp {
  let comp = lazyIconCache.get(key);
  if (!comp) {
    comp = lazy(async () => {
      const m = await import("lucide-react");
      const Icon = (m.icons as Record<string, LucideIcon>)[key] || m.Puzzle;
      return { default: Icon as ComponentType<{ size?: number }> };
    });
    lazyIconCache.set(key, comp);
  }
  return comp;
}

export function pluginViewIcon(name?: string, size = 18): ReactNode {
  if (!name) return <Puzzle size={size} />;
  const Curated = PLUGIN_VIEW_ICONS[name];
  if (Curated) return <Curated size={size} />;
  const Lazy = lazyLucideIcon(toPascalCase(name));
  return (
    <Suspense fallback={<Puzzle size={size} />}>
      <Lazy size={size} />
    </Suspense>
  );
}
