// Fleet quick-chat palette entries (#1733). The command palette gets an "Agents" section listing
// every OTHER fleet agent; picking one navigates to that agent's slug-routed console, where you
// chat with it immediately. Reachability comes straight from the roster: `running` IS the remote's
// reachability probe (see FleetManagerPanel), so a down/unreachable agent is shown disabled.
import type { FleetAgent } from "../lib/types";

export type FleetPaletteEntry = {
  id: string;
  slug: string;
  label: string;
  hint: string;
  disabled: boolean;
  keywords: string[];
};

const RECENCY_KEY = "protoagent.fleet.recent";

/** Last-opened timestamp per agent slug (localStorage) — powers "recently chatted sorts first". */
export function readAgentRecency(): Record<string, number> {
  try {
    const raw = localStorage.getItem(RECENCY_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Record<string, number>) : {};
  } catch {
    return {};
  }
}

/** Record that an agent was just opened from the palette so it sorts to the top next time. */
export function markAgentOpened(slug: string, now: number = Date.now()): void {
  try {
    const r = readAgentRecency();
    r[slug] = now;
    localStorage.setItem(RECENCY_KEY, JSON.stringify(r));
  } catch {
    /* localStorage unavailable — recency is a nicety, never let it block the open */
  }
}

/** Build the palette's fleet entries from the live roster: every agent EXCEPT the one this window
 *  is focused on, ordered reachable-first → most-recently-opened → alphabetical. Down agents stay
 *  in the list but disabled (you can't chat with one that isn't up). The host entry's slug is the
 *  literal "host" (ADR 0042 routing), not its `id`. */
export function fleetPaletteEntries(
  agents: FleetAgent[],
  currentSlug: string,
  recency: Record<string, number> = {},
): FleetPaletteEntry[] {
  return agents
    .map((a) => {
      const slug = a.host ? "host" : a.id;
      const reachable = a.running;
      const hint = reachable ? (a.remote ? "remote · switch" : "switch") : a.remote ? "unreachable" : "stopped";
      return {
        id: `fleet:${slug}`,
        slug,
        label: a.name,
        hint,
        disabled: !reachable,
        keywords: ["fleet", "agent", "chat", "switch", a.name, slug],
      };
    })
    .filter((e) => e.slug !== currentSlug) // you're already on this one — its own "Chat with…" covers it
    .sort(
      (a, b) =>
        Number(a.disabled) - Number(b.disabled) || // reachable before down
        (recency[b.slug] ?? 0) - (recency[a.slug] ?? 0) || // most-recently-opened next
        a.label.localeCompare(b.label), // then alphabetical
    );
}

/** The fleet agents whose live process can be toggled on/off from the palette (#1769).
 *  ON/OFF is the live process state (`FleetAgent.running`), not a persisted flag: "on" =
 *  `POST /api/fleet/<name>/start`, "off" = `POST /api/fleet/<name>/stop`. Only LOCAL,
 *  non-host members qualify — the SAME gate FleetManagerPanel uses to show Start/Stop:
 *   • the host serves this console, so stopping it would kill the session — never listed;
 *   • a REMOTE member has no local process here (its `/start|/stop` 400s) — excluded too.
 *  Sorted stably by display name so the picker order is deterministic across polls. */
export function togglableFleetAgents(agents: FleetAgent[]): FleetAgent[] {
  return agents
    .filter((a) => !a.host && !a.remote)
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name));
}
