import { useEffect, useRef, useState } from "react";

// Cost bounds for inline artifact frames (ADR 0118 D2). This module is framework-light
// pure logic, reused by the inline artifact host (S7b), the streamed preview (S8) and the
// frame-component host (S12): it owns the height clamp, the per-chat-view live-frame budget
// and the lazy-mount wrapper, with no UI wiring of its own.
//
// The height clamp and the near-viewport lazy-mount rule are adapted from CopilotKit's
// OpenIntelligentUI ("OIU", https://github.com/CopilotKit/OpenIntelligentUI, MIT, @ f6e4388).

/** The inline frame height floor — a frame reporting less (or nothing) is pinned here. */
export const MIN_FRAME_HEIGHT = 80;
/** The inline frame height ceiling — anything taller scrolls inside the frame. */
export const MAX_FRAME_HEIGHT = 1200;

/** Clamp a frame's reported content height to [80, 1200] px. A non-finite value — `NaN`,
 *  `±Infinity`, or a frame that never measured — floors to 80, never the ceiling. */
export function clampHeight(h: number): number {
  if (!Number.isFinite(h)) return MIN_FRAME_HEIGHT;
  if (h < MIN_FRAME_HEIGHT) return MIN_FRAME_HEIGHT;
  if (h > MAX_FRAME_HEIGHT) return MAX_FRAME_HEIGHT;
  return h;
}

// ── live-frame budget ────────────────────────────────────────────────────────────────

/** At most this many inline frames are live at once in one chat view. */
export const MAX_LIVE_FRAMES = 6;

/** A frame evicted to make room: the caller renders it as a static "Click to resume" card
 *  at `height` — its last measured content height — so the transcript layout doesn't jump. */
export type EvictedFrame = { id: string; height: number };

type FrameEntry = { id: string; recency: number; height: number };

/** A per-chat-view registry of live inline frames, bounded at `cap`. Registering a frame
 *  past the cap evicts the least-recently-visible one. "Visible" is driven by `touch` (the
 *  host calls it when a frame scrolls into view), so a frame that scrolled away long ago is
 *  the one that goes. A frame's last measured height is remembered across eviction, so a
 *  resumed (re-registered) frame can restore its size before it remeasures. */
export type FrameRegistry = {
  /** Max live frames; evictions keep `size()` at or below it. */
  readonly cap: number;
  /** Register a holder of `id` as live. Returns the frame evicted to stay within `cap`
   *  (its id + last measured height), or `null` when there was room or `id` was already
   *  live. Reference-counted: each holder is one claim (two mounts of one component share
   *  an id), so a second register on a live id consumes NO new slot and evicts nothing — it
   *  only refreshes visibility recency. */
  register(id: string): EvictedFrame | null;
  /** Mark `id` visible now, so it is not the next frame evicted. No-op for an unknown id. */
  touch(id: string): void;
  /** Record `id`'s reported content height (clamped). Returns the clamped value, and
   *  remembers it for the resume card even after `id` is evicted or released. */
  measure(id: string, height: number): number;
  /** Release one holder's claim on `id` (its frame unmounted). The slot is dropped from the
   *  live set only when the LAST holder releases — a surviving co-id mount keeps it live. A
   *  no-op once `id` has no live claims (e.g. already evicted). Remembered height is kept. */
  release(id: string): void;
  /** The last height remembered for `id`, or the floor if it never measured. */
  heightOf(id: string): number;
  /** Is `id` currently a live frame? */
  isLive(id: string): boolean;
  /** The live frame ids (unordered). */
  liveIds(): string[];
  /** How many frames are live now. */
  size(): number;
};

export function createFrameRegistry(cap: number = MAX_LIVE_FRAMES): FrameRegistry {
  const live = new Map<string, FrameEntry>();
  // How many mounted hosts currently claim each id — the AUTHORITATIVE reference count,
  // kept independent of the live-slot map so it survives eviction. Two mounts of one
  // component share an id (see FrameComponentHostProps), so `register`/`release` come in
  // pairs per host and a slot is freed only when the LAST holder releases. Driving release
  // off this count (not the live entry) also means a stale release from an evicted holder
  // can never drop a freshly re-registered sibling's slot. Deleted at zero, so no leak.
  const claims = new Map<string, number>();
  // Heights persist past eviction/release so a resume card — or a re-registered frame —
  // keeps its last size. Scoped to one chat view's registry, cleared when it is discarded.
  const heights = new Map<string, number>();
  let tick = 0;
  const bump = () => ++tick;

  function evictLeastRecentlyVisible(): EvictedFrame | null {
    let victim: FrameEntry | null = null;
    for (const entry of live.values()) {
      if (!victim || entry.recency < victim.recency) victim = entry;
    }
    if (!victim) return null;
    live.delete(victim.id);
    return { id: victim.id, height: victim.height };
  }

  return {
    cap,
    register(id) {
      claims.set(id, (claims.get(id) ?? 0) + 1);
      const existing = live.get(id);
      if (existing) {
        // Another holder of an already-live id: shares the one slot, so no new slot is
        // consumed and nothing is evicted — just refresh its visibility recency.
        existing.recency = bump();
        return null;
      }
      const evicted = live.size >= cap ? evictLeastRecentlyVisible() : null;
      live.set(id, { id, recency: bump(), height: heights.get(id) ?? MIN_FRAME_HEIGHT });
      return evicted;
    },
    touch(id) {
      const entry = live.get(id);
      if (entry) entry.recency = bump();
    },
    measure(id, height) {
      const clamped = clampHeight(height);
      heights.set(id, clamped);
      const entry = live.get(id);
      if (entry) entry.height = clamped;
      return clamped;
    },
    release(id) {
      const remaining = (claims.get(id) ?? 0) - 1;
      if (remaining > 0) {
        // A co-id mount is still here — keep the slot live for the survivor.
        claims.set(id, remaining);
        return;
      }
      // Last holder gone (or an id with no claims — e.g. one already evicted): clear the
      // claim and free its live slot. `live.delete` is a no-op if it was already evicted.
      claims.delete(id);
      live.delete(id);
    },
    heightOf(id) {
      return heights.get(id) ?? MIN_FRAME_HEIGHT;
    },
    isLive(id) {
      return live.has(id);
    },
    liveIds() {
      return [...live.keys()];
    },
    size() {
      return live.size;
    },
  };
}

// ── lazy mount ───────────────────────────────────────────────────────────────────────

/** How near the viewport a frame must scroll before it mounts. A positive margin mounts it
 *  just before it is on screen, so it is ready as it scrolls in. */
export const LAZY_MOUNT_ROOT_MARGIN = "200px";

/** A handle to stop observing a not-yet-mounted frame (e.g. on unmount). */
export type LazyMountHandle = { disconnect(): void };

/** Call `onEnter` once, the first time `el` scrolls within `rootMargin` of the viewport,
 *  then stop observing — mounting is one-way, the registry governs the frame's life after.
 *  Where IntersectionObserver is unavailable (SSR, an old runtime) the frame mounts eagerly,
 *  so nothing is ever stuck unmounted. */
export function observeLazyMount(
  el: Element,
  onEnter: () => void,
  rootMargin: string = LAZY_MOUNT_ROOT_MARGIN,
): LazyMountHandle {
  if (typeof IntersectionObserver === "undefined") {
    onEnter();
    return { disconnect() {} };
  }
  let fired = false;
  const io = new IntersectionObserver(
    (entries) => {
      if (fired) return;
      if (entries.some((entry) => entry.isIntersecting)) {
        fired = true;
        io.disconnect();
        onEnter();
      }
    },
    { rootMargin },
  );
  io.observe(el);
  return {
    disconnect() {
      io.disconnect();
    },
  };
}

/** React hook form of {@link observeLazyMount}: attach `ref` to the frame's placeholder and
 *  render the real frame only once `mounted` flips true. Mounting is sticky — once true it
 *  stays true, even if the frame later scrolls away (the registry, not this hook, evicts). */
export function useLazyMount<T extends Element = HTMLElement>(rootMargin: string = LAZY_MOUNT_ROOT_MARGIN) {
  const ref = useRef<T | null>(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (mounted) return;
    const el = ref.current;
    if (!el) return;
    const handle = observeLazyMount(el, () => setMounted(true), rootMargin);
    return () => handle.disconnect();
  }, [mounted, rootMargin]);
  return { ref, mounted };
}
