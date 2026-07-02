import { topicMatches } from "./events";

// The pure half of the PluginView iframe event bridge (#1640): parse an untrusted
// `protoagent:subscribe` postMessage, then relay ring-buffer replay + live bus events
// to the sandboxed page with seq-based dedupe. Extracted from PluginView so the
// replay-then-live ordering contract is unit-testable without a DOM.
//
// Protocol (page → host):
//   { type: "protoagent:subscribe", patterns: ["a.#"], since?: <seq>, background?: <bool> }
// - `patterns` replace the previous set (as before #1640).
// - `since` asks for immediate replay of retained events with seq > since (the console's
//   client-side mirror of the server ring, lib/events.ts `replaySince`).
// - `background` requests hidden delivery — handled by the HOST (PluginView reports it to
//   the ui store; the App keeps the view mounted), not here.
//
// Host → page: every relayed frame is `{ type: "protoagent:event", topic, data, seq }` —
// `seq` is the page's high-water mark for its next `since`.

export type RelayFrame = { topic: string; data: Record<string, unknown>; seq?: number };

export type SubscribeRequest = {
  patterns: string[];
  since?: number;
  background?: boolean;
};

/** Parse a `protoagent:subscribe` postMessage payload. Returns null when the message
 *  isn't a subscribe at all; malformed optional fields are dropped, not rejected —
 *  a pre-#1640 subscribe (`{patterns}` only) parses to exactly the old behavior. */
export function parseSubscribe(m: unknown): SubscribeRequest | null {
  if (!m || typeof m !== "object") return null;
  const msg = m as Record<string, unknown>;
  if (msg.type !== "protoagent:subscribe" || !Array.isArray(msg.patterns)) return null;
  const req: SubscribeRequest = {
    patterns: msg.patterns.filter((p): p is string => typeof p === "string"),
  };
  if (typeof msg.since === "number" && Number.isFinite(msg.since)) req.since = msg.since;
  if (typeof msg.background === "boolean") req.background = msg.background;
  return req;
}

export type PluginEventRelay = {
  /** Apply a subscribe: replace patterns; when `since` is present, replay retained
   *  matching frames newer than it (and reset the dedupe mark to the page's `since` —
   *  the page is authoritative about what it has). */
  subscribe: (req: SubscribeRequest) => void;
  /** Offer a live bus event; relayed iff it matches a pattern and its seq is above the
   *  high-water mark (never the same seq twice past a replay). */
  deliver: (topic: string, data: Record<string, unknown>, seq?: number) => void;
};

export function createPluginEventRelay(opts: {
  /** Post one `protoagent:event` frame to the page. */
  post: (frame: RelayFrame) => void;
  /** Retained frames with seq > since, oldest→newest (lib/events.ts `replaySince`). */
  replaySince: (since: number) => RelayFrame[];
}): PluginEventRelay {
  let patterns: string[] = [];
  // Highest seq relayed to (or claimed by) the page — the replay/live dedupe line.
  // null until the page names a `since` or a seq'd live event is relayed.
  let highWater: number | null = null;
  const matches = (topic: string) => patterns.some((p) => topicMatches(p, topic));

  return {
    subscribe(req) {
      patterns = req.patterns;
      if (req.since === undefined) return;
      // Page-authoritative reset: replay everything retained past ITS mark, even if a
      // prior subscription already relayed some of it — a page that re-subscribes with
      // an older `since` is saying its model only reflects up to that seq.
      let hw = req.since;
      for (const f of opts.replaySince(req.since)) {
        if (typeof f.seq !== "number" || f.seq <= hw) continue;
        if (!matches(f.topic)) continue;
        opts.post({ topic: f.topic, data: f.data, seq: f.seq });
        hw = f.seq;
      }
      highWater = hw;
    },
    deliver(topic, data, seq) {
      if (!matches(topic)) return;
      if (typeof seq === "number") {
        // Replay in `subscribe` runs synchronously inside the message handler, and
        // dispatch retains-before-fanout — so a live frame is either ≤ highWater
        // (already replayed) or new. Drop the former, advance on the latter.
        if (highWater !== null && seq <= highWater) return;
        highWater = seq;
        opts.post({ topic, data, seq });
        return;
      }
      // A frame without a seq (not produced by the server bus) can't be deduped or
      // tracked — relay as-is so nothing is silently dropped.
      opts.post({ topic, data });
    },
  };
}
