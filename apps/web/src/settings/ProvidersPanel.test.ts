import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import {
  buildReleases,
  CLEAR_TARGET,
  CONNECTION_TYPE_OPTIONS,
  defaultReferenceSelections,
  humaniseReferenceKey,
  ID_RE,
  otherConnectionGroups,
  providerDraftForType,
  providerIdError,
  referencesResolved,
} from "./ProvidersPanel";

type Entry = { key: string; value: string | string[]; kind: "slot" | "favorite" | "subagent"; clearable: boolean };

const IN_USE: Entry[] = [
  { key: "routing.aux_model", value: "gw:aux", kind: "slot", clearable: true },
  { key: "model.name", value: "gw:lead", kind: "slot", clearable: false },
  { key: "model.favorites", value: ["gw:a", "gw:b"], kind: "favorite", clearable: true },
  { key: "subagents.coder.model", value: "gw:c", kind: "subagent", clearable: true },
];

// The console refuses locally what the route would refuse anyway, so a typo is caught
// before a round trip. This rule MIRRORS `valid_provider_id` in graph/config.py — if the
// two drift, an id the backend rejects only surfaces as a 400 from a form that said it
// was fine.
describe("provider id validation mirrors the backend", () => {
  it("accepts the ids the registry allows", () => {
    for (const ok of ["gateway", "prod-gateway", "local_vllm", "g1", "openai-codex"]) {
      expect(ID_RE.test(ok)).toBe(true);
      expect(providerIdError(ok)).toBe("");
    }
  });

  it("rejects anything the <provider>:<model> grammar could not survive", () => {
    // A colon would break the split; a slash collides with gateway aliases.
    for (const bad of ["has:colon", "has/slash", "Upper", "-leading", "has space", "é"]) {
      expect(ID_RE.test(bad)).toBe(false);
      expect(providerIdError(bad)).toMatch(/reserved by the provider:model grammar/);
    }
  });

  it("says nothing about an empty field — that is not an error yet, just unfinished", () => {
    expect(providerIdError("")).toBe("");
  });
});

describe("new connection types", () => {
  it("offers gateway, Claude, and Codex connections", () => {
    expect(CONNECTION_TYPE_OPTIONS.map((option) => option.value)).toEqual([
      "openai-compat",
      "anthropic-oauth",
      "openai-codex",
    ]);
  });

  it("prefills OAuth ids and never asks those connections for endpoint credentials", () => {
    expect(providerDraftForType("anthropic-oauth")).toEqual({
      id: "anthropic-oauth",
      type: "anthropic-oauth",
      label: "Claude",
      base_url: "",
      api_key: "",
    });
    expect(providerDraftForType("openai-codex")).toEqual({
      id: "openai-codex",
      type: "openai-codex",
      label: "ChatGPT / Codex",
      base_url: "",
      api_key: "",
    });
  });
});

// ── Resolve-references helpers (bd-v6xy) ───────────────────────────────────────
describe("humaniseReferenceKey", () => {
  it("gives the fixed slots friendly names", () => {
    expect(humaniseReferenceKey("model.name")).toBe("Lead model");
    expect(humaniseReferenceKey("routing.aux_model")).toBe("Auxiliary model");
    expect(humaniseReferenceKey("model.favorites")).toBe("Favorite models");
  });

  it("reads a subagent slot as 'Subagent <name> model'", () => {
    expect(humaniseReferenceKey("subagents.researcher.model")).toBe("Subagent researcher model");
  });

  it("degrades an unknown dotted key to readable spacing", () => {
    expect(humaniseReferenceKey("foo.bar_baz")).toBe("foo · bar baz");
  });
});

describe("defaultReferenceSelections — clearable slots start at Clear", () => {
  it("defaults every clearable slot to Clear and leaves model.name / favorites unset", () => {
    expect(defaultReferenceSelections(IN_USE)).toEqual({
      "routing.aux_model": CLEAR_TARGET,
      "subagents.coder.model": CLEAR_TARGET,
    });
  });
});

describe("referencesResolved — the primary-button gate", () => {
  it("is false until the non-clearable model.name has a concrete target", () => {
    expect(referencesResolved(IN_USE, defaultReferenceSelections(IN_USE))).toBe(false);
  });

  it("is true once model.name is repointed, regardless of the clearable defaults", () => {
    const selections = { ...defaultReferenceSelections(IN_USE), "model.name": "local:x" };
    expect(referencesResolved(IN_USE, selections)).toBe(true);
  });

  it("a Clear (sentinel) never counts as resolving model.name", () => {
    const selections = { ...defaultReferenceSelections(IN_USE), "model.name": CLEAR_TARGET };
    expect(referencesResolved(IN_USE, selections)).toBe(false);
  });
});

describe("otherConnectionGroups — repoint options exclude the connection being removed", () => {
  it("drops the removed connection's lane and keeps the rest, grouped by connection", () => {
    const groups = otherConnectionGroups(["gateway:a", "gateway:b", "local-vllm:c"], "gateway");
    expect(groups.map((g) => g.lane)).toEqual(["local-vllm"]);
    expect(groups[0].items).toEqual(["local-vllm:c"]);
  });

  it("keeps every lane when the removed id names none of them", () => {
    const groups = otherConnectionGroups(["gateway:a", "local-vllm:c"], "unrelated");
    expect(groups.map((g) => g.lane).sort()).toEqual(["gateway", "local-vllm"]);
  });
});

describe("buildReleases — Clear → null, favorites → null, chosen lane → <pid>:<model>", () => {
  it("maps each reference from the selections exactly", () => {
    const selections = { ...defaultReferenceSelections(IN_USE), "model.name": "local:x" };
    expect(buildReleases(IN_USE, selections)).toEqual({
      "routing.aux_model": null, // clearable, left at its Clear default
      "model.name": "local:x", // repointed to another connection
      "model.favorites": null, // a favorites entry can only be cleared
      "subagents.coder.model": null, // clearable, left at its Clear default
    });
  });

  it("carries a repointed clearable slot's qualified lane through", () => {
    const selections = {
      ...defaultReferenceSelections(IN_USE),
      "routing.aux_model": "local:y",
      "model.name": "local:x",
    };
    expect(buildReleases(IN_USE, selections)["routing.aux_model"]).toBe("local:y");
  });

  it("omits a non-clearable reference with nothing chosen (never emits a clear it would 400 on)", () => {
    const releases = buildReleases(IN_USE, defaultReferenceSelections(IN_USE));
    expect("model.name" in releases).toBe(false);
  });
});

// ── Client contract: the DELETE body is present ONLY when releases are given ────
describe("api.removeProvider request body (bd-v6xy)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/app/"); // host window — no slug prefix
  });

  function capture() {
    const calls: { url: string; method?: string; body?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), method: init?.method, body: init?.body as string | undefined });
        return new Response(JSON.stringify({ ok: true, removed: "gw" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    return calls;
  }

  it("sends NO body for the bare call (byte-identical to the old refuse-if-in-use path)", async () => {
    const calls = capture();
    await api.removeProvider("gw", false);
    expect(calls[0].url).toBe("/api/config/providers/gw");
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].body).toBeUndefined();
  });

  it("still sends no body when only confirm_last is set", async () => {
    const calls = capture();
    await api.removeProvider("gw", true);
    expect(calls[0].url).toBe("/api/config/providers/gw?confirm_last=true");
    expect(calls[0].body).toBeUndefined();
  });

  it("sends { releases } as JSON when releases are provided", async () => {
    const calls = capture();
    await api.removeProvider("gw", false, { "routing.aux_model": null, "model.name": "local:x" });
    expect(calls[0].method).toBe("DELETE");
    expect(JSON.parse(calls[0].body as string)).toEqual({
      releases: { "routing.aux_model": null, "model.name": "local:x" },
    });
  });

  it("carries confirm_last=true alongside the releases body for a last-connection removal", async () => {
    const calls = capture();
    await api.removeProvider("gw", true, { "model.favorites": null });
    expect(calls[0].url).toBe("/api/config/providers/gw?confirm_last=true");
    expect(JSON.parse(calls[0].body as string)).toEqual({ releases: { "model.favorites": null } });
  });
});
