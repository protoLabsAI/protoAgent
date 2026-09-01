// Palette frecency store — what the operator actually reaches for.
//
// WHY A NEW KEY. `app/fleetPalette.ts` already holds `protoagent.fleet.recent`, but it is a
// FLAT map of agent slug → timestamp with exactly one writer (`markAgentOpened`), and
// nothing has ever recorded COMMAND usage. Reusing it would have shipped a read-only
// "recents" list that never learns, and worse: a plugin command with id "ava" would clobber
// agent ava's recency, because a flat map has no namespace. So this is a new key with typed,
// prefixed entries — `cmd:<id>` and `agent:<slug>` — and `fleetPalette`'s key and exact
// shape are left untouched (`fleetPalette.test.ts` asserts `toEqual({ ava: 100, bob: 200 })`,
// so ANY wrapper or sub-map under the old key would red it).
//
// GLOBAL, NOT PER-AGENT-SLUG — a deliberate split from `uiStore`, whose persist IS
// slug-suffixed (`protoagent.ui:<slug>`, see `_layoutStorage`). Layout is per-agent because
// each agent's docks are its own; command habits are the OPERATOR's, and three things fall
// out of that: the frameless desktop launcher window has no slug at all and would otherwise
// start cold forever; the commands that dominate a recents list (Settings, Plugins:
// Discover, Fleet Room) are the same in every window; and the key this migrates FROM is
// already global. An id that only exists in one window (a plugin view another agent doesn't
// run) simply doesn't resolve there and is skipped when the list is rendered.
const KEY = "protoagent.palette.recent";
/** `fleetPalette.ts`'s store — the one-time migration source. Read, never written. */
const LEGACY_FLEET_KEY = "protoagent.fleet.recent";

/** Uses + last use. Frequency alone pins a command you hammered once in March to the top;
 *  recency alone forgets the thing you run every morning the moment you try something new. */
export type RecentEntry = { n: number; t: number };
export type RecentMap = Record<string, RecentEntry>;

/** Namespaced keys — the whole reason for the new store. */
export const commandKey = (id: string) => `cmd:${id}`;
export const agentKey = (slug: string) => `agent:${slug}`;

/** A recorded use is worth half as much after a week. */
const HALF_LIFE_MS = 7 * 24 * 60 * 60 * 1000;
/** Keep the store bounded — an operator's tail is noise, and this is parsed on every open. */
const MAX_ENTRIES = 120;

/** Frecency: uses, decayed by how long ago the last one was. 0 for "never used". */
export function frecency(entry: RecentEntry | undefined, now: number = Date.now()): number {
  if (!entry) return 0;
  const age = Math.max(0, now - entry.t);
  return Math.max(1, entry.n) * Math.pow(0.5, age / HALF_LIFE_MS);
}

/** Keep only well-shaped entries — this is parsed from localStorage, which any older build
 *  (or another tab) may have written. */
function coerce(raw: unknown): RecentMap {
  if (!raw || typeof raw !== "object") return {};
  const out: RecentMap = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const e = v as Partial<RecentEntry> | null;
    if (!e || typeof e !== "object") continue;
    if (typeof e.t !== "number" || !Number.isFinite(e.t)) continue;
    out[k] = { n: typeof e.n === "number" && e.n > 0 ? e.n : 1, t: e.t };
  }
  return out;
}

/** Seed the new store from `protoagent.fleet.recent` (slug → timestamp), one time. Every
 *  legacy timestamp becomes a single use of `agent:<slug>`. Exported for the test.
 *
 *  NOTHING READS `agent:*` YET, and this comment says so rather than implying otherwise: the
 *  Fleet Room roster sorts host → running → alphabetical on purpose (`FleetRoom.tsx`, so the
 *  3s poll can't reorder rows under the cursor), and the empty-query list only reads `cmd:`.
 *  The migration still runs NOW because the new key's EXISTENCE is the one-shot marker —
 *  seeding on the day a reader lands would need a second, separate migration flag to avoid
 *  re-importing timestamps the operator has since moved past. Write half now, read half when
 *  there is a surface that wants it. */
export function migrateFleetRecency(): RecentMap {
  try {
    const raw = localStorage.getItem(LEGACY_FLEET_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (!parsed || typeof parsed !== "object") return {};
    const out: RecentMap = {};
    for (const [slug, t] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof t === "number" && Number.isFinite(t)) out[agentKey(slug)] = { n: 1, t };
    }
    return out;
  } catch {
    return {};
  }
}

function write(map: RecentMap): void {
  const entries = Object.entries(map);
  const now = Date.now();
  // Last-used breaks the tie, and it is not decoration: `frecency` UNDERFLOWS to 0 for
  // anything far enough in the past (0.5^n with a large n), so a store full of stale
  // entries would otherwise prune in insertion order and evict the row just written.
  const kept =
    entries.length <= MAX_ENTRIES
      ? entries
      : entries
          .sort((a, b) => frecency(b[1], now) - frecency(a[1], now) || b[1].t - a[1].t)
          .slice(0, MAX_ENTRIES);
  localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(kept)));
}

/** The whole store. On the FIRST read (no key yet) this migrates the fleet store forward
 *  and persists the result, so the migration happens once rather than on every read. */
export function readPaletteRecency(): RecentMap {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw != null) return coerce(JSON.parse(raw));
    const seeded = migrateFleetRecency();
    // Write even when EMPTY: the key's existence is the migration marker, and re-parsing
    // the legacy key on every palette open just to find nothing is pure waste.
    write(seeded);
    return seeded;
  } catch {
    // localStorage unavailable (private mode, a locked-down webview) — recency is a
    // nicety; the palette must open regardless.
    return {};
  }
}

/** Record a use of a namespaced key (`cmd:…` / `agent:…`). */
export function markPaletteUsed(key: string, now: number = Date.now()): void {
  if (!key) return;
  try {
    const map = readPaletteRecency();
    const prev = map[key];
    map[key] = { n: (prev?.n ?? 0) + 1, t: now };
    write(map);
  } catch {
    /* see readPaletteRecency — never let the store block the command */
  }
}

/** Record that a command was RUN. Called from the root view's single `run()` — so no command
 *  SOURCE can forget to feed it — and from `withRecency`, which the adapter wraps a submorph's
 *  own list in (a submorph is a DS view; the root's chokepoint does not reach inside it). */
export function markCommandUsed(id: string, now?: number): void {
  markPaletteUsed(commandKey(id), now);
}

/** Record that an agent was opened (the Fleet Room roster's "open console" action). */
export function markAgentUsed(slug: string, now?: number): void {
  markPaletteUsed(agentKey(slug), now);
}
