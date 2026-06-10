import { lazy, Suspense, type ComponentType, type LazyExoticComponent, type ReactNode } from "react";

import { Package, type LucideIcon } from "lucide-react";

// Resolve ANY lucide icon by name (e.g. an archetype/plugin-supplied "Sparkles",
// "LayoutGrid") — lazily, so the full lucide set lands in a separate chunk that only
// loads when a non-curated glyph is actually used. Falls back to Package on an unknown
// name. Shared by the fleet archetype picker + any surface that renders backend-named icons.

function toPascalCase(s: string): string {
  return s.replace(/(^|[-_\s])(\w)/g, (_m, _sep, c: string) => c.toUpperCase());
}

type IconComp = LazyExoticComponent<ComponentType<{ size?: number }>>;
const cache = new globalThis.Map<string, IconComp>();

function lazyIcon(key: string): IconComp {
  let comp = cache.get(key);
  if (!comp) {
    comp = lazy(async () => {
      const m = await import("lucide-react");
      const Icon = (m.icons as Record<string, LucideIcon>)[key] || m.Package;
      return { default: Icon as ComponentType<{ size?: number }> };
    });
    cache.set(key, comp);
  }
  return comp;
}

export function lucideIcon(name: string | undefined, size = 18): ReactNode {
  if (!name) return <Package size={size} />;
  const Lazy = lazyIcon(toPascalCase(name));
  return (
    <Suspense fallback={<Package size={size} />}>
      <Lazy size={size} />
    </Suspense>
  );
}
