// The pure half of the PluginView iframe KEYBINDING bridge (#1457). Extracted from
// PluginView so the trust rules are unit-testable without a DOM — the same split
// `pluginEventRelay` uses.
//
// A plugin view is a sandboxed iframe, which breaks keyboard shortcuts in both
// directions: keys pressed inside it never reach the host's window listener (so host
// chords are dead while the view has focus), and the page can't reach
// `registerKeybinding` to declare its own. Two message types close that:
//
//   page → host  { type: "protoagent:keybindings", bindings: [{id, label, group?, keys}] }
//   page → host  { type: "protoagent:keydown", combo: "mod+k", editable?: bool }
//   host → page  { type: "protoagent:keybinding", id: "<the plugin's own id>" }
//
// Trust, mirroring the `protoagent:publish` namespace rule (a page can only publish under
// its own id): a plugin's binding ids are FORCED into its own `plugin.<pluginId>.`
// namespace here, so a page cannot register — or silently replace — a core binding like
// `chat.new`, and cannot collide with another plugin. The chord it asks for is only a
// DEFAULT; the operator's override always wins, through the same Settings ▸ Keyboard path
// as everything else.

/** A binding a plugin page asked the host to register, already namespaced. */
export type PluginBindingSpec = {
  /** `plugin.<pluginId>.<their id>` — forced, never taken verbatim from the page. */
  id: string;
  /** The id as the PAGE knows it; echoed back when the chord fires. */
  pluginLocalId: string;
  label: string;
  group: string;
  defaultKeys: string;
};

const MAX_BINDINGS = 32; // a view declaring hundreds of chords is a bug or an attack
const ID_OK = /^[a-zA-Z0-9._-]+$/;

/** Parse a `protoagent:keybindings` message into namespaced specs.
 *
 * Returns null when the message isn't a keybinding registration at all. A malformed
 * ENTRY is dropped rather than failing the batch — one bad chord shouldn't cost a plugin
 * its whole keyboard surface. An empty array is meaningful: it clears the page's
 * bindings (a view re-registering with fewer). */
export function parsePluginKeybindings(m: unknown, pluginId: string): PluginBindingSpec[] | null {
  if (!m || typeof m !== "object") return null;
  const msg = m as Record<string, unknown>;
  if (msg.type !== "protoagent:keybindings" || !Array.isArray(msg.bindings)) return null;
  if (!pluginId) return null; // no namespace to force ids into ⇒ refuse the whole batch

  const out: PluginBindingSpec[] = [];
  const seen = new Set<string>();
  for (const raw of msg.bindings.slice(0, MAX_BINDINGS)) {
    if (!raw || typeof raw !== "object") continue;
    const b = raw as Record<string, unknown>;
    const localId = typeof b.id === "string" ? b.id.trim() : "";
    const keys = typeof b.keys === "string" ? b.keys.trim().toLowerCase() : "";
    if (!localId || !keys || !ID_OK.test(localId)) continue;
    const id = `plugin.${pluginId}.${localId}`;
    if (seen.has(id)) continue; // first declaration wins, like the registry's own dedup
    seen.add(id);
    out.push({
      id,
      pluginLocalId: localId,
      label: (typeof b.label === "string" && b.label.trim()) || localId,
      group: (typeof b.group === "string" && b.group.trim()) || "Plugins",
      defaultKeys: keys,
    });
  }
  return out;
}

export type ForwardedKey = { combo: string; editable: boolean };

/** Parse a `protoagent:keydown` message — a chord the page didn't handle, offered to the
 *  host so a global shortcut still works while the view has focus.
 *
 * `editable` is the PAGE's claim that focus is in one of its own text fields; the host
 * honours it so ⌘K doesn't fire out from under someone typing in a plugin's search box.
 * Defaults to false when absent (an older kit) — the same default the host applies to a
 * non-editable target. */
export function parseForwardedKey(m: unknown): ForwardedKey | null {
  if (!m || typeof m !== "object") return null;
  const msg = m as Record<string, unknown>;
  if (msg.type !== "protoagent:keydown" || typeof msg.combo !== "string") return null;
  const combo = msg.combo.trim().toLowerCase();
  if (!combo) return null;
  return { combo, editable: msg.editable === true };
}
