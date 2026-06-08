import type { ContextMenuRegistration, ContextType, MenuEntry } from "./types";

// The registry (ADR 0036 D2): core AND plugins register menus per ContextType; the menu for a
// type is the merged union of all registrations, priority-sorted and deduped by id.
const registrations = new Map<string, ContextMenuRegistration[]>();

export function registerContextMenu(reg: ContextMenuRegistration): () => void {
  const list = registrations.get(reg.type) || [];
  list.push(reg);
  list.sort((a, b) => (b.priority || 0) - (a.priority || 0));
  registrations.set(reg.type, list);
  return () => {
    const cur = registrations.get(reg.type) || [];
    registrations.set(reg.type, cur.filter((r) => r !== reg));
  };
}

export function resolveMenu(type: ContextType, ctx: unknown): MenuEntry[] {
  const out: MenuEntry[] = [];
  const seen = new Set<string>();
  for (const reg of registrations.get(type) || []) {
    const items = typeof reg.items === "function" ? reg.items(ctx) : reg.items;
    for (const it of items) {
      if (!seen.has(it.id)) { seen.add(it.id); out.push(it); }
    }
  }
  return out;
}
