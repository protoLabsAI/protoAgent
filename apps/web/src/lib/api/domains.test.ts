import { describe, expect, it } from "vitest";
import { api } from "../api";
import { automationApi } from "./automation";
import { chatApi } from "./chat";
import { fleetApi } from "./fleet";
import { knowledgeApi } from "./knowledge";
import { pluginsApi } from "./plugins";
import { runtimeApi } from "./runtime";
import { setupApi } from "./setup";
import { telemetryApi } from "./telemetry";
import { workspaceApi } from "./workspace";

// #3822: `api` is the spread of these slices. A method name defined in two slices would
// silently shadow (the later spread wins), so assert the slices are disjoint and that the
// composed object is exactly their union — a new slice not listed here fails the count.
const SLICES = {
  automationApi,
  chatApi,
  fleetApi,
  knowledgeApi,
  pluginsApi,
  runtimeApi,
  setupApi,
  telemetryApi,
  workspaceApi,
};

describe("api domain slices", () => {
  it("define every method name exactly once", () => {
    const owner = new Map<string, string>();
    const dups: string[] = [];
    for (const [slice, obj] of Object.entries(SLICES)) {
      for (const key of Object.keys(obj)) {
        if (owner.has(key)) dups.push(`${key} (${owner.get(key)} + ${slice})`);
        owner.set(key, slice);
      }
    }
    expect(dups).toEqual([]);
    expect(Object.keys(api).sort()).toEqual([...owner.keys()].sort());
  });

  it("compose into api by reference", () => {
    for (const obj of Object.values(SLICES)) {
      for (const [key, fn] of Object.entries(obj)) {
        expect((api as Record<string, unknown>)[key]).toBe(fn);
      }
    }
  });
});
