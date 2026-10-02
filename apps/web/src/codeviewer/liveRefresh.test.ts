import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createChangeCoalescer,
  diffQueryKey,
  invalidateCodePane,
  parseFsChanged,
  REFRESH_DEBOUNCE_MS,
  REFRESH_MAX_WAIT_MS,
  type ChangeBatch,
} from "./liveRefresh";

// The code pane's live half (ADR 0112): `fs.changed` frames are debounced and coalesced
// into ONE re-fetch per burst — a coder settling a dozen edits a second must not fire a
// dozen `git diff`s — and land in the query cache as the pane's own keys.

describe("parseFsChanged", () => {
  it("normalizes a bus payload and rejects one without a project", () => {
    expect(parseFsChanged({ project: "app", paths: ["./src//a.ts", 3, ""], source: "delegate", target: "claude-code" })).toEqual({
      project: "app",
      paths: ["src/a.ts"],
      source: "delegate",
      target: "claude-code",
    });
    expect(parseFsChanged({ project: "app" })).toEqual({ project: "app", paths: [], source: "" });
    expect(parseFsChanged({ paths: ["a"] })).toBeNull();
    expect(parseFsChanged(null)).toBeNull();
  });
});

describe("createChangeCoalescer", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("a burst flushes ONCE, after the quiet period, with the union of its paths", () => {
    const flushes: ChangeBatch[] = [];
    const c = createChangeCoalescer((b) => flushes.push(b));
    c.note("app", ["a.ts"]);
    vi.advanceTimersByTime(100);
    c.note("app", ["b.ts", "a.ts"]);
    c.note("docs", ["x.md"]);
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS - 1);
    expect(flushes).toHaveLength(0);
    vi.advanceTimersByTime(1);
    expect(flushes).toHaveLength(1);
    expect([...(flushes[0].get("app") as Set<string>)].sort()).toEqual(["a.ts", "b.ts"]);
    expect([...(flushes[0].get("docs") as Set<string>)]).toEqual(["x.md"]);
    vi.advanceTimersByTime(5_000);
    expect(flushes).toHaveLength(1);
  });

  it("a change naming no path widens the project to 'all', and stays widened", () => {
    const flushes: ChangeBatch[] = [];
    const c = createChangeCoalescer((b) => flushes.push(b));
    c.note("app", ["a.ts"]);
    c.note("app", []);
    c.note("app", ["b.ts"]);
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(flushes[0].get("app")).toBe("all");
  });

  it("a non-stop stream still refreshes every REFRESH_MAX_WAIT_MS", () => {
    const flushes: ChangeBatch[] = [];
    const c = createChangeCoalescer((b) => flushes.push(b));
    for (let t = 0; t < REFRESH_MAX_WAIT_MS * 2; t += 100) {
      c.note("app", [`f${t}.ts`]);
      vi.advanceTimersByTime(100);
    }
    expect(flushes.length).toBeGreaterThanOrEqual(2);
  });

  it("dispose drops a pending batch", () => {
    const onFlush = vi.fn();
    const c = createChangeCoalescer(onFlush);
    c.note("app", ["a.ts"]);
    c.dispose();
    vi.advanceTimersByTime(REFRESH_MAX_WAIT_MS);
    expect(onFlush).not.toHaveBeenCalled();
  });
});

describe("invalidateCodePane", () => {
  const seeded = () => {
    const qc = new QueryClient();
    qc.setQueryData(diffQueryKey("app"), { files: [] });
    qc.setQueryData(diffQueryKey("docs"), { files: [] });
    qc.setQueryData(["code-pane-file", "app", "src/a.ts", 0, 1, 0, 0], { text: "a" });
    qc.setQueryData(["code-pane-file", "app", "src/b.ts", 0, 1, 0, 0], { text: "b" });
    return qc;
  };
  const stale = (qc: QueryClient, key: readonly unknown[]) => qc.getQueryState(key)?.isInvalidated ?? false;

  it("named paths re-fetch that project's diff and just those files", () => {
    const qc = seeded();
    invalidateCodePane(qc, new Map([["app", new Set(["src/a.ts"])]]));
    expect(stale(qc, diffQueryKey("app"))).toBe(true);
    expect(stale(qc, diffQueryKey("docs"))).toBe(false);
    expect(stale(qc, ["code-pane-file", "app", "src/a.ts", 0, 1, 0, 0])).toBe(true);
    expect(stale(qc, ["code-pane-file", "app", "src/b.ts", 0, 1, 0, 0])).toBe(false);
  });

  it("'all' re-fetches every cached file of the project", () => {
    const qc = seeded();
    invalidateCodePane(qc, new Map([["app", "all" as const]]));
    expect(stale(qc, ["code-pane-file", "app", "src/b.ts", 0, 1, 0, 0])).toBe(true);
  });
});
