import { describe, expect, it } from "vitest";

import { createStreamFollower, type ScrollBox } from "./streamFollow";

/** A scroll box with real clamping, so scrollTop behaves like the browser's. */
function box(content: number, viewport = 500) {
  let top = 0;
  const b = {
    content,
    get scrollHeight() {
      return Math.max(b.content, viewport);
    },
    clientHeight: viewport,
    get scrollTop() {
      return top;
    },
    set scrollTop(v: number) {
      top = Math.max(0, Math.min(v, b.scrollHeight - viewport));
    },
  };
  return b as ScrollBox & { content: number };
}

const dist = (b: ScrollBox) => b.scrollHeight - b.scrollTop - b.clientHeight;

describe("createStreamFollower", () => {
  it("lands at the bottom and follows growth while pinned", () => {
    const b = box(2000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    expect(dist(b)).toBe(0);
    b.content += 300;
    f.follow();
    expect(dist(b)).toBe(0);
    expect(f.pinned).toBe(true);
  });

  it("stays pinned when a tall card grows between the follow scroll and its scroll event (B6)", () => {
    const b = box(2000);
    const changes: boolean[] = [];
    const f = createStreamFollower(b, (p) => changes.push(p));
    f.pin();
    b.content += 40; // streamed text
    f.follow(); // the observer scrolls to the bottom...
    b.content += 450; // ...an artifact iframe measures itself before the scroll event lands
    f.onScroll(); // the event for the follower's own scroll: 450px from the bottom
    expect(f.pinned).toBe(true);
    f.follow(); // the observer sees the card's growth and keeps following
    expect(dist(b)).toBe(0);
    expect(changes).toEqual([]);
  });

  it("stops following when the user scrolls up, and growth no longer moves the view", () => {
    const b = box(2000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    b.scrollTop -= 400;
    f.onScroll();
    expect(f.pinned).toBe(false);
    const top = b.scrollTop;
    b.content += 600;
    f.follow();
    expect(b.scrollTop).toBe(top);
  });

  it("a small upward nudge that stays within the threshold keeps following", () => {
    const b = box(2000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    b.scrollTop -= 10;
    f.onScroll();
    expect(f.pinned).toBe(true);
  });

  it("scrolling back to the bottom re-pins", () => {
    const b = box(2000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    b.scrollTop -= 400;
    f.onScroll();
    expect(f.pinned).toBe(false);
    b.scrollTop = b.scrollHeight;
    f.onScroll();
    expect(f.pinned).toBe(true);
  });

  it("a send re-pins from anywhere", () => {
    const b = box(3000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    b.scrollTop = 0;
    f.onScroll();
    expect(f.pinned).toBe(false);
    f.pin();
    expect(f.pinned).toBe(true);
    expect(dist(b)).toBe(0);
  });

  it("an animated jump is not unpinned by its own in-flight scroll events while content grows", () => {
    const b = box(3000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    b.scrollTop = 0;
    f.onScroll();
    let target = 0;
    f.pin((t) => {
      target = t;
    });
    // Mid-animation frames move DOWN while the answer keeps growing.
    b.scrollTop = 1000;
    f.onScroll();
    b.content += 200;
    b.scrollTop = 2000;
    f.onScroll();
    expect(f.pinned).toBe(true);
    expect(target).toBe(3000);
    f.follow(); // growth during the animation is caught up instantly
    expect(dist(b)).toBe(0);
  });

  it("a restored (upward) scroll position unpins", () => {
    const b = box(3000);
    const f = createStreamFollower(b, () => {});
    f.pin();
    b.scrollTop = 800; // the slot's scroll memory restoring your place
    f.onScroll();
    expect(f.pinned).toBe(false);
  });

  it("layout moving scrollTop up (scroll anchoring) while pinned is caught up, not read as intent", () => {
    const b = box(2000);
    const f = createStreamFollower(b, () => {}, () => false);
    f.pin();
    b.content -= 40; // a card above settles shorter...
    b.scrollTop -= 200; // ...and anchoring shifts the view up to hold its anchor
    f.onScroll();
    expect(f.pinned).toBe(true);
    expect(dist(b)).toBe(0);
  });

  it("the same upward move made by the user unpins", () => {
    const b = box(2000);
    const f = createStreamFollower(b, () => {}, () => true);
    f.pin();
    b.scrollTop -= 200;
    f.onScroll();
    expect(f.pinned).toBe(false);
  });

  it("a non-user upward move while already unpinned leaves the view alone", () => {
    let user = true;
    const b = box(3000);
    const f = createStreamFollower(b, () => {}, () => user);
    f.pin();
    b.scrollTop = 1000;
    f.onScroll();
    user = false;
    b.scrollTop = 900; // anchoring while reading back
    f.onScroll();
    expect(f.pinned).toBe(false);
    expect(b.scrollTop).toBe(900);
  });
});
