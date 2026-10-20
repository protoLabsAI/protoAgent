import { useCallback, useLayoutEffect, useRef, useState } from "react";

// Follow-the-stream for the chat transcript (B6). Replaces the DS `Conversation`'s built-in
// autoScroll, which re-derived "pinned" from EVERY scroll event's distance-to-bottom. A
// scroll event is dispatched a frame after the scroll that caused it, so when a tall card —
// an artifact iframe measuring itself, a lazy-mounted frame, a chart — grew the thread in
// between, the event for the follower's OWN scroll read "far from the bottom" and unpinned.
// Every later turn then landed off-screen behind the jump button. The same thing cut a
// smooth jump short whenever the answer grew mid-animation.
//
// The rule here: following is dropped only by the USER moving the view up — an upward
// scroll while they're touching it (wheel, touch, keyboard, scrollbar drag, or the slot's
// scroll-memory restore, which marks itself via `markUserScroll`). Layout can move
// scrollTop up on its own too — browser scroll anchoring when a card above the viewport
// shrinks — and while pinned that is caught up to the bottom instead of read as intent.
// Reaching the bottom re-pins, as before; so does sending a message (`signal`), always.

/** Within this many px of the bottom counts as "at the bottom" (the DS's own threshold). */
export const PIN_THRESHOLD = 32;

/** How long after an input gesture a scroll still counts as the user's. */
export const USER_SCROLL_WINDOW_MS = 800;

const lastUserScroll = new WeakMap<HTMLElement, number>();

/** Mark the next scroll of `el` as the user's own — for code that scrolls on their behalf
 *  (restoring a remembered position), so the follower lets go instead of snapping back. */
export function markUserScroll(el: HTMLElement): void {
  lastUserScroll.set(el, performance.now());
}

/** The slice of a scroll container the follower reads and writes. */
export type ScrollBox = { scrollTop: number; readonly scrollHeight: number; readonly clientHeight: number };

export type StreamFollower = {
  readonly pinned: boolean;
  /** Content or viewport resized: stay at the bottom when pinned. */
  follow(): void;
  /** A scroll event. Unpins only on a user's upward move that leaves the bottom. */
  onScroll(): void;
  /** An explicit upward gesture (wheel up) — stop following now, before the scroll lands. */
  release(): void;
  /** Re-pin and land at the bottom (a send, or the jump button). */
  pin(scrollTo?: (top: number) => void): void;
};

export function createStreamFollower(
  box: ScrollBox,
  onPinnedChange: (pinned: boolean) => void,
  /** Is the user scrolling right now (a gesture just landed)? */
  userScrolling: () => boolean = () => true,
): StreamFollower {
  let pinned = true;
  let lastTop = box.scrollTop;
  const setPinned = (next: boolean) => {
    if (next === pinned) return;
    pinned = next;
    onPinnedChange(next);
  };
  const toBottom = () => {
    box.scrollTop = box.scrollHeight;
    lastTop = box.scrollTop; // the clamped value — what the coming scroll event will report
  };
  return {
    get pinned() {
      return pinned;
    },
    follow() {
      if (pinned) toBottom();
    },
    onScroll() {
      const top = box.scrollTop;
      if (box.scrollHeight - top - box.clientHeight < PIN_THRESHOLD) setPinned(true);
      else if (top < lastTop - 1) {
        if (userScrolling()) setPinned(false);
        else if (pinned) {
          toBottom(); // layout (scroll anchoring) moved us, not the user — keep following
          return;
        }
      }
      lastTop = top;
    },
    release() {
      setPinned(false);
    },
    pin(scrollTo) {
      setPinned(true);
      if (scrollTo) {
        // Animated: the in-flight scroll only ever moves down, so it can't unpin; growth
        // during it is followed instantly by `follow`.
        lastTop = box.scrollTop;
        scrollTo(box.scrollHeight);
      } else {
        toBottom();
      }
    },
  };
}

/**
 * Drive the follower for the DS `Conversation` rendered with `autoScroll={false}` under
 * `#rootId`. Returns whether it's pinned, the wrap element (the jump button's positioning
 * context — portal it there) and a `jump` action. Bumping `signal` re-pins (a send).
 */
export function useStreamFollow(rootId: string, signal: number) {
  const [pinned, setPinned] = useState(true);
  const [wrap, setWrap] = useState<HTMLElement | null>(null);
  const followerRef = useRef<StreamFollower | null>(null);
  const scrollRef = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    const root = document.getElementById(rootId);
    const scroller = root?.querySelector<HTMLElement>(".pl-convo-scroll");
    const content = root?.querySelector<HTMLElement>(".pl-convo");
    if (!root || !scroller || !content) return;
    setWrap(root);
    scrollRef.current = scroller;
    // While pinned, browser scroll anchoring is off: when a card above the viewport SHRINKS
    // (a streamed preview settling into its final frame, a tool group collapsing) anchoring
    // moves scrollTop UP to hold the anchor in place, which reads exactly like the user
    // scrolling away. Unpinned (reading back), anchoring stays on so your place holds.
    const anchor = (isPinned: boolean) => {
      scroller.style.overflowAnchor = isPinned ? "none" : "";
    };
    let pointerDown = false;
    const userScrolling = () =>
      pointerDown || performance.now() - (lastUserScroll.get(scroller) ?? -Infinity) < USER_SCROLL_WINDOW_MS;
    const follower = createStreamFollower(
      scroller,
      (next) => {
        anchor(next);
        setPinned(next);
      },
      userScrolling,
    );
    anchor(true);
    followerRef.current = follower;
    follower.pin(); // land at the bottom on mount
    const onScroll = () => follower.onScroll();
    const mark = () => markUserScroll(scroller);
    const onWheel = (e: WheelEvent) => {
      mark();
      // Only a wheel that can actually move the view up — not one at the top already.
      if (e.deltaY < 0 && scroller.scrollTop > 0) follower.release();
    };
    // A scrollbar drag holds the pointer for as long as it likes; count all of it.
    const onPointerDown = () => {
      pointerDown = true;
      mark();
    };
    const onPointerUp = () => {
      if (!pointerDown) return;
      pointerDown = false;
      mark();
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    scroller.addEventListener("wheel", onWheel, { passive: true });
    scroller.addEventListener("touchmove", mark, { passive: true });
    scroller.addEventListener("keydown", mark);
    // Keyboard scrolling (PageUp, Home, arrows, space) also lands here from a focused body.
    const onDocKey = (e: KeyboardEvent) => {
      if (/^(PageUp|PageDown|Home|End|ArrowUp|ArrowDown| )$/.test(e.key)) mark();
    };
    document.addEventListener("keydown", onDocKey);
    scroller.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    // The content column (streamed text, cards, iframes measuring themselves) AND the
    // viewport (the composer growing, a HITL card docking) — either can open a gap.
    const ro = new ResizeObserver(() => follower.follow());
    ro.observe(content);
    ro.observe(scroller);
    return () => {
      ro.disconnect();
      scroller.removeEventListener("scroll", onScroll);
      scroller.removeEventListener("wheel", onWheel);
      scroller.removeEventListener("touchmove", mark);
      scroller.removeEventListener("keydown", mark);
      document.removeEventListener("keydown", onDocKey);
      scroller.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
      followerRef.current = null;
      scrollRef.current = null;
      anchor(false);
    };
  }, [rootId]);

  const firstSignal = useRef(true);
  useLayoutEffect(() => {
    if (firstSignal.current) {
      firstSignal.current = false;
      return;
    }
    followerRef.current?.pin();
  }, [signal]);

  const jump = useCallback(() => {
    const el = scrollRef.current;
    followerRef.current?.pin(el ? (top) => el.scrollTo({ top, behavior: "smooth" }) : undefined);
  }, []);

  return { pinned, wrap, jump };
}
