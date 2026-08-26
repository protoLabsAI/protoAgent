import { describe, expect, it } from "vitest";

import { CONNECTION_TYPE_OPTIONS, ID_RE, providerDraftForType, providerIdError } from "./ProvidersPanel";

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
