import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import {
  clampZoom,
  readStoredZoom,
  applyZoom,
  zoomIn,
  zoomOut,
  zoomReset,
  initZoom,
  ZOOM_MIN,
  ZOOM_MAX,
  ZOOM_DEFAULT,
} from "./zoom";

const KEY = "pl:ui-zoom";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("zoom");
});
afterEach(() => vi.restoreAllMocks());

describe("clampZoom", () => {
  it("clamps to range and rounds to one decimal (no float drift)", () => {
    expect(clampZoom(0.1)).toBe(ZOOM_MIN);
    expect(clampZoom(5)).toBe(ZOOM_MAX);
    expect(clampZoom(1.2345)).toBe(1.2);
    expect(clampZoom(Number.NaN)).toBe(ZOOM_DEFAULT);
  });
});

describe("zoom in / out / reset", () => {
  it("steps by 0.1 and persists the level", () => {
    expect(zoomIn()).toBe(1.1);
    expect(localStorage.getItem(KEY)).toBe("1.1");
    expect(zoomIn()).toBe(1.2);
    expect(zoomOut()).toBe(1.1);
  });

  it("clears storage when back at the default (no leftover zoom:1)", () => {
    zoomIn();
    expect(zoomOut()).toBe(ZOOM_DEFAULT);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("never exceeds MAX or drops below MIN", () => {
    for (let i = 0; i < 40; i++) zoomIn();
    expect(readStoredZoom()).toBe(ZOOM_MAX);
    for (let i = 0; i < 40; i++) zoomOut();
    expect(readStoredZoom()).toBe(ZOOM_MIN);
  });

  it("reset returns to default and clears storage", () => {
    zoomIn();
    zoomIn();
    expect(zoomReset()).toBe(ZOOM_DEFAULT);
    expect(localStorage.getItem(KEY)).toBeNull();
  });
});

describe("applyZoom → document element", () => {
  it("sets the zoom property, and clears it at the default", () => {
    const setP = vi.spyOn(document.documentElement.style, "setProperty");
    const rmP = vi.spyOn(document.documentElement.style, "removeProperty");
    applyZoom(1.4);
    expect(setP).toHaveBeenCalledWith("zoom", "1.4");
    applyZoom(ZOOM_DEFAULT);
    expect(rmP).toHaveBeenCalledWith("zoom");
  });
});

describe("initZoom", () => {
  it("applies the persisted level on boot", () => {
    localStorage.setItem(KEY, "1.3");
    const setP = vi.spyOn(document.documentElement.style, "setProperty");
    initZoom();
    expect(setP).toHaveBeenCalledWith("zoom", "1.3");
  });

  it("tolerates storage being unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(() => initZoom()).not.toThrow();
    expect(readStoredZoom()).toBe(ZOOM_DEFAULT);
  });
});
