// The pure half of the PluginView iframe CONTEXT-MENU bridge (#3030). Extracted from
// PluginView so the trust rules are unit-testable without a DOM — the same split
// `pluginEventRelay` and `pluginKeybindings` use.
//
// A plugin view is a sandboxed iframe, so the console's context-menu system (ADR 0036)
// stopped at its edge: `registerContextMenu` is host-side JS the page can't reach, and a
// right-click inside the frame fires in the frame's own document — it never bubbles to the
// host, and under the desktop app the frame is cross-origin, so the host can't listen on
// its `contentDocument` either. The page is therefore the only party that can see the
// right-click; these messages let it hand that event to the console's menu:
//
//   page → host  { type: "protoagent:contextmenu:register", items: [...] }
//   page → host  { type: "protoagent:contextmenu:open", x, y, items?: [...] }
//   host → page  { type: "protoagent:contextmenu:action", itemId: "<the page's own id>" }
//
// The page calls `preventDefault()` on its own `contextmenu` event (suppressing the
// browser menu) and posts `:open` with the cursor position in ITS viewport; the host
// translates through the iframe's rect and opens the console menu there. `:register`
// declares a default set once, so a page that always shows the same menu can post a bare
// `:open`. Re-registering REPLACES the previous set (post `items: []` to clear), so a view
// that drops an item doesn't leave a ghost entry firing into a page that forgot about it.
//
// Trust, mirroring the `protoagent:publish` namespace rule (a page can only publish under
// its own id) and the keybinding bridge: item ids are FORCED into the plugin's own
// `plugin.<pluginId>.` namespace here, so a page can neither register over a core menu
// item (`configure`, `uninstall`, …) nor collide with another plugin; the action posted
// back carries the id the PAGE knows. Nothing executable crosses the boundary — an item is
// a label, an optional lucide icon NAME (resolved host-side against the console's own icon
// set, never plugin-supplied markup), and two booleans.

/** One item a plugin page contributed to a console context menu, already namespaced. */
export type PluginMenuItem = {
  /** `plugin.<pluginId>.<their id>` — forced, never taken verbatim from the page. */
  id: string;
  /** The id as the PAGE knows it; echoed back when the item is chosen. */
  pluginLocalId: string;
  label: string;
  /** A lucide icon name (PascalCase or kebab-case), resolved host-side. */
  icon?: string;
  /** Renders in the DS destructive style. */
  danger?: boolean;
  disabled?: boolean;
};
export type PluginMenuDivider = { id: string; divider: true };
export type PluginMenuEntry = PluginMenuItem | PluginMenuDivider;

const MAX_ITEMS = 32; // a view declaring hundreds of entries is a bug or an attack
const MAX_LABEL = 120; // …and one with a paragraph for a label would blow out the menu
const ID_OK = /^[a-zA-Z0-9._-]+$/;
// Lucide names as the console's resolver accepts them: `LineChart` / `line-chart`.
const ICON_OK = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/;

/** Parse a page-supplied `items` array into namespaced entries.
 *
 * A malformed ENTRY is dropped rather than failing the batch — one bad item shouldn't cost
 * a plugin its whole menu. Leading/trailing/repeated dividers are collapsed, so a page that
 * builds its list conditionally can't end up with a menu that opens on a separator. */
export function parsePluginMenuItems(items: unknown, pluginId: string): PluginMenuEntry[] | null {
  if (!Array.isArray(items)) return null;
  if (!pluginId) return null; // no namespace to force ids into ⇒ refuse the whole batch

  const out: PluginMenuEntry[] = [];
  const seen = new Set<string>();
  let pendingDivider = false;
  for (const raw of items.slice(0, MAX_ITEMS)) {
    if (!raw || typeof raw !== "object") continue;
    const it = raw as Record<string, unknown>;
    if (it.divider === true) {
      pendingDivider = out.length > 0; // never lead with a separator
      continue;
    }
    const localId = typeof it.id === "string" ? it.id.trim() : "";
    if (!localId || !ID_OK.test(localId)) continue;
    const id = `plugin.${pluginId}.${localId}`;
    if (seen.has(id)) continue; // first declaration wins, like the registry's own dedup
    seen.add(id);
    if (pendingDivider) {
      out.push({ id: `${id}.__divider`, divider: true });
      pendingDivider = false;
    }
    const label = typeof it.label === "string" ? it.label.trim().slice(0, MAX_LABEL) : "";
    const icon = typeof it.icon === "string" ? it.icon.trim() : "";
    out.push({
      id,
      pluginLocalId: localId,
      label: label || localId,
      ...(icon && ICON_OK.test(icon) ? { icon } : {}),
      ...(it.danger === true ? { danger: true } : {}),
      ...(it.disabled === true ? { disabled: true } : {}),
    });
  }
  return out; // a trailing divider was never pushed — `pendingDivider` is simply dropped
}

/** Parse a `protoagent:contextmenu:register` message — the page's DEFAULT item set.
 *
 * Returns null when the message isn't a menu registration at all. An empty array is
 * meaningful: it clears the page's items. */
export function parsePluginMenuRegistration(m: unknown, pluginId: string): PluginMenuEntry[] | null {
  if (!m || typeof m !== "object") return null;
  const msg = m as Record<string, unknown>;
  if (msg.type !== "protoagent:contextmenu:register") return null;
  return parsePluginMenuItems(msg.items, pluginId);
}

export type PluginMenuOpen = {
  /** Cursor position in the IFRAME's viewport — the host translates through its rect. */
  x: number;
  y: number;
  /** The items for THIS menu, or null to fall back to the registered set. */
  entries: PluginMenuEntry[] | null;
};

/** Parse a `protoagent:contextmenu:open` message — "the operator right-clicked here".
 *
 * Coordinates are the page's own `clientX/clientY`; a missing or non-finite one falls back
 * to the frame's top-left rather than refusing the menu (the operator asked for it — a
 * slightly mispositioned menu beats none). Omitting `items` opens the registered set. */
export function parsePluginMenuOpen(m: unknown, pluginId: string): PluginMenuOpen | null {
  if (!m || typeof m !== "object") return null;
  const msg = m as Record<string, unknown>;
  if (msg.type !== "protoagent:contextmenu:open") return null;
  if (!pluginId) return null;
  const coord = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0);
  return {
    x: coord(msg.x),
    y: coord(msg.y),
    entries: msg.items === undefined ? null : parsePluginMenuItems(msg.items, pluginId),
  };
}
