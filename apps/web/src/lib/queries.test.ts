import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { currentSlug } from "./api";
import {
  goalDetailQuery,
  goalsQuery,
  hostRuntimeStatusQuery,
  knowledgeQuery,
  queryKeys,
  settingsSchemaQuery,
} from "./queries";

// Slug-namespaced cache keys (#2887). The QueryClient is shared per window, so every
// key leads with the focused agent's slug — read from the URL at ACCESS time, never
// module init. Today an agent switch is a full page navigation (the cache dies with
// the page); these tests guard the hazard an in-place switch would hit: agent A's
// cached rows served under agent B for a staleTime window.

// Drive the URL the way the console does (/app/agent/<slug>/) and always restore it,
// so the jsdom location shared by this file never leaks between tests.
const atPath = <T>(path: string, fn: () => T): T => {
  window.history.replaceState(null, "", path);
  try {
    return fn();
  } finally {
    window.history.replaceState(null, "", "/");
  }
};

describe("slug-namespaced query keys (#2887)", () => {
  it("prefixes every key with the focused agent's slug", () => {
    atPath("/app/agent/roxy/", () => {
      expect(currentSlug()).toBe("roxy");
      for (const [name, entry] of Object.entries(queryKeys)) {
        const key = typeof entry === "function" ? entry("s1" as never) : entry;
        expect(key[0], `queryKeys.${name}`).toBe("roxy");
      }
    });
  });

  it("falls back to the host namespace outside an agent path", () => {
    expect(queryKeys.goals).toEqual(["host", "goals"]);
    expect(queryKeys.settings).toEqual(["host", "settings", "schema"]);
  });

  it("reads the slug at access time, not module init", () => {
    expect(queryKeys.goals).toEqual(["host", "goals"]);
    expect(atPath("/app/agent/gina/chat", () => queryKeys.goals)).toEqual(["gina", "goals"]);
    // ...and back — nothing was captured when the module loaded.
    expect(queryKeys.goals).toEqual(["host", "goals"]);
  });

  it("bakes the slug into keys built by the option factories", () => {
    expect(atPath("/app/agent/roxy/", () => goalsQuery().queryKey)).toEqual(["roxy", "goals"]);
    expect(atPath("/app/agent/roxy/", () => settingsSchemaQuery().queryKey)).toEqual([
      "roxy",
      "settings",
      "schema",
    ]);
    expect(atPath("/app/agent/roxy/", () => knowledgeQuery("faq").queryKey)).toEqual([
      "roxy",
      "knowledge",
      "faq",
    ]);
  });

  it("keeps parent keys as prefixes of their children", () => {
    atPath("/app/agent/roxy/", () => {
      const isPrefixOf = (parent: readonly unknown[], child: readonly unknown[]) =>
        parent.every((part, i) => child[i] === part);
      expect(isPrefixOf(queryKeys.goals, goalDetailQuery("s1").queryKey)).toBe(true);
      expect(isPrefixOf(queryKeys.workflowRuns, queryKeys.workflowRun("r1"))).toBe(true);
      expect(isPrefixOf(queryKeys.telemetry, queryKeys.fleetTelemetry)).toBe(true);
      expect(isPrefixOf(queryKeys.runtime, queryKeys.nodeRuntime)).toBe(true);
      expect(isPrefixOf(queryKeys.runtime, queryKeys.pythonRuntime)).toBe(true);
      expect(isPrefixOf(queryKeys.delegates, queryKeys.delegateTypes)).toBe(true);
      expect(isPrefixOf(queryKeys.memory, queryKeys.memoryInjections)).toBe(true);
      expect(isPrefixOf(queryKeys.memoryInjections, queryKeys.memoryInjectionDetail(7))).toBe(true);
    });
  });

  it("subtree invalidation refreshes children without crossing agents", async () => {
    const qc = new QueryClient();
    const roxyDetail = atPath("/app/agent/roxy/", () => queryKeys.goalDetail("s1"));
    const hostDetail = queryKeys.goalDetail("s1");
    qc.setQueryData(roxyDetail, { status: "roxy" });
    qc.setQueryData(hostDetail, { status: "host" });
    // The panel's bus-push invalidation, as fired from roxy's window.
    await atPath("/app/agent/roxy/", () => qc.invalidateQueries({ queryKey: queryKeys.goals }));
    expect(qc.getQueryState(roxyDetail)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(hostDetail)?.isInvalidated).toBe(false);
    qc.clear();
  });

  it("keeps the host runtime probe outside the slug namespace on purpose", () => {
    expect(atPath("/app/agent/roxy/", () => hostRuntimeStatusQuery().queryKey)).toEqual([
      "runtime",
      "host",
    ]);
  });
});
