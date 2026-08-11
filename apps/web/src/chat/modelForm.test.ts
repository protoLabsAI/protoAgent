import { describe, expect, it } from "vitest";

import type { SettingsGroup } from "../lib/types";
import {
  bareModel,
  groupByLane,
  laneOf,
  modelCardHint,
  modelChoices,
  modelFormPayload,
  modelPickerData,
  providerOf,
  resolveModelArg,
} from "./modelForm";

const field = (key: string, value: unknown, options: string[] = []) => ({
  key,
  label: key,
  type: "string" as const,
  section: "Model",
  restart: false,
  options,
  value,
});

function groups(favorites: unknown, models: string[] = ["protolabs/reasoning", "protolabs/fast"]): SettingsGroup[] {
  return [
    {
      section: "Model",
      category: "Model",
      fields: [field("model.name", "protolabs/reasoning", models)],
    },
    {
      section: "Favorite models",
      category: "Model",
      fields: [field("model.favorites", favorites, models)],
    },
  ] as SettingsGroup[];
}

describe("modelPickerData — schema extraction", () => {
  it("reads favorites (ordered, deduped), the full list, and the configured default", () => {
    const data = modelPickerData(groups(["protolabs/fast", "protolabs/fast", "openai/gpt"]));
    expect(data.favorites).toEqual(["protolabs/fast", "openai/gpt"]);
    expect(data.models).toEqual(["protolabs/reasoning", "protolabs/fast"]);
    expect(data.globalModel).toBe("protolabs/reasoning");
  });

  it("tolerates a missing favorites field and junk values (fork/older-backend schema)", () => {
    const noFavs = modelPickerData([groups(undefined)[0]]);
    expect(noFavs.favorites).toEqual([]);
    expect(modelPickerData(groups([42, "", "real"])).favorites).toEqual(["real"]);
  });

  it("falls back to the saved model when the gateway list is empty (same as the composer picker)", () => {
    expect(modelPickerData(groups([], [])).models).toEqual(["protolabs/reasoning"]);
  });
});

describe("providerOf / modelCardHint", () => {
  it("derives the provider from the alias prefix", () => {
    expect(providerOf("openai/gpt-5.2")).toBe("openai");
    expect(providerOf("protolabs/reasoning/deepseek")).toBe("protolabs");
    expect(providerOf("gpt-5.2")).toBe("");
    expect(providerOf("/weird")).toBe("");
  });

  it("hints the provider and marks the configured default", () => {
    expect(modelCardHint("openai/gpt", "protolabs/reasoning")).toBe("openai");
    expect(modelCardHint("protolabs/reasoning", "protolabs/reasoning")).toBe("protolabs · configured default");
    expect(modelCardHint("bare-alias", "")).toBe("gateway model");
  });
});

describe("modelFormPayload — the /model card form", () => {
  type ModelField = { oneOf: { const: string; description?: string }[]; default?: string };
  const modelField = (p: ReturnType<typeof modelFormPayload>): ModelField =>
    (p.steps![0].schema as { properties: Record<string, unknown> }).properties.model as ModelField;

  it("offers ONLY the favorites (in order) when any are pinned", () => {
    const data = modelPickerData(groups(["protolabs/fast", "protolabs/reasoning"]));
    const payload = modelFormPayload(data, "protolabs/reasoning");
    expect(modelChoices(data).fromFavorites).toBe(true);
    expect(modelField(payload).oneOf.map((o) => o.const)).toEqual(["protolabs/fast", "protolabs/reasoning"]);
    expect(payload.description).toContain("Manage favorites");
  });

  it("falls back to the FULL list with a pin-favorites hint when none are set", () => {
    const data = modelPickerData(groups([]));
    const payload = modelFormPayload(data, "protolabs/reasoning");
    expect(modelField(payload).oneOf.map((o) => o.const)).toEqual(["protolabs/reasoning", "protolabs/fast"]);
    expect(payload.description).toContain("No favorites pinned");
  });

  it("preselects the tab's current model only when it's among the cards", () => {
    const data = modelPickerData(groups(["protolabs/fast"]));
    expect(modelField(modelFormPayload(data, "protolabs/fast")).default).toBe("protolabs/fast");
    expect(modelField(modelFormPayload(data, "protolabs/reasoning")).default).toBeUndefined();
  });

  it("cards carry the provider hint + configured-default marker", () => {
    const data = modelPickerData(groups(["protolabs/fast", "protolabs/reasoning"]));
    const cards = modelField(modelFormPayload(data, "")).oneOf;
    expect(cards[0].description).toBe("protolabs");
    expect(cards[1].description).toBe("protolabs · configured default");
  });
});

describe("resolveModelArg — the typed /model <alias> path", () => {
  const data = modelPickerData(groups(["protolabs/fast"]));

  it("matches case-insensitively against favorites ∪ the full list, returning the canonical alias", () => {
    expect(resolveModelArg(data, "PROTOLABS/FAST")).toBe("protolabs/fast");
    expect(resolveModelArg(data, "protolabs/reasoning")).toBe("protolabs/reasoning");
  });

  it("rejects unknown aliases (typo protection) and blanks", () => {
    expect(resolveModelArg(data, "protolabs/typo")).toBeNull();
    expect(resolveModelArg(data, "  ")).toBeNull();
  });

  it("trusts the typed alias when NO models are known at all (gateway list unavailable)", () => {
    const empty = modelPickerData([]);
    expect(empty.models).toEqual([]);
    expect(resolveModelArg(empty, "anything/goes")).toBe("anything/goes");
  });
});

// #2473 — native OAuth subscription models are not "gateway models": the cards,
// the source hint, and the no-favorites description must all say subscription.
describe("native OAuth provider labeling (#2473)", () => {
  const codexGroups = (models: string[]): SettingsGroup[] =>
    [
      {
        section: "Model",
        category: "Model",
        fields: [
          field("model.name", "gpt-5.6-sol", models),
          field("model.provider", "openai-codex"),
        ],
      },
    ] as SettingsGroup[];

  it("extracts the provider and lists every discovered subscription model", () => {
    const data = modelPickerData(codexGroups(["gpt-5.6-sol", "gpt-5-codex", "o4-mini"]));
    expect(data.provider).toBe("openai-codex");
    expect(modelChoices(data).choices).toEqual(["gpt-5.6-sol", "gpt-5-codex", "o4-mini"]);
  });

  it("labels subscription cards as the subscription, never 'gateway model'", () => {
    expect(modelCardHint("gpt-5.6-sol", "gpt-5.6-sol", "openai-codex")).toBe(
      "ChatGPT subscription · configured default",
    );
    expect(modelCardHint("claude-sonnet-4-5", "", "anthropic-oauth")).toBe("Claude subscription");
    // Gateway aliases keep their prefix; unknown providers keep the old wording.
    expect(modelCardHint("openai/gpt-5.2", "", "openai-codex")).toBe("openai");
    expect(modelCardHint("bare-model", "", "")).toBe("gateway model");
  });

  it("the no-favorites description names the subscription, not the gateway", () => {
    const data = modelPickerData(codexGroups(["gpt-5.6-sol", "gpt-5-codex"]));
    const payload = modelFormPayload(data, "gpt-5.6-sol");
    expect(payload.description).toContain("ChatGPT subscription");
    expect(payload.description).not.toContain("gateway");
  });
});

// ── cross-provider picker (part C) ────────────────────────────────────────────
// Part A made `<provider>:<model>` a routable slot value; part B made the settings
// schema offer those names across every signed-in lane. The picker has to speak that
// grammar without showing it to the operator: the VALUE stays qualified (it's what gets
// applied and saved), the card reads as a plain model name, and the lane moves to the hint.
function crossGroups(favorites: unknown, crossProvider: string[]): SettingsGroup[] {
  return [
    {
      section: "Model",
      category: "Model",
      fields: [field("model.name", "protolabs/reasoning", ["protolabs/reasoning"])],
    },
    {
      section: "Favorite models",
      category: "Model",
      fields: [field("model.favorites", favorites, crossProvider)],
    },
  ] as SettingsGroup[];
}

const LANES = [
  "gateway:protolabs/coder",
  "anthropic-oauth:claude-sonnet-5",
  "openai-codex:gpt-5.6-sol",
];

describe("lane parsing mirrors the backend's split_slot_target", () => {
  it("recognises the three lanes and strips the prefix", () => {
    expect(laneOf("anthropic-oauth:claude-sonnet-5")).toBe("anthropic-oauth");
    expect(bareModel("anthropic-oauth:claude-sonnet-5")).toBe("claude-sonnet-5");
    expect(bareModel("gateway:protolabs/coder")).toBe("protolabs/coder");
  });

  it("leaves unqualified names alone — a gateway alias is NOT a lane", () => {
    for (const plain of ["protolabs/coder", "claude-sonnet-5", "openai/gpt-5.2", ""]) {
      expect(laneOf(plain)).toBe("");
      expect(bareModel(plain)).toBe(plain);
    }
    // An unknown prefix is part of the model name, not a lane — same rule as the backend.
    expect(laneOf("bedrock:anthropic.claude")).toBe("");
  });
});

describe("the picker shows models, not slot syntax", () => {
  it("titles the card with the bare id and keeps the qualified value", () => {
    const data = modelPickerData(crossGroups(LANES, LANES));
    const payload = modelFormPayload(data, "");
    const cards = (
      (payload.steps![0].schema as { properties: Record<string, unknown> }).properties.model as {
        oneOf: { const: string; title: string; description: string }[];
      }
    ).oneOf;

    expect(cards.map((c) => c.title)).toEqual(["protolabs/coder", "claude-sonnet-5", "gpt-5.6-sol"]);
    expect(cards.map((c) => c.const)).toEqual(LANES); // what actually gets applied
    expect(cards[1].description).toContain("Claude subscription");
    expect(cards[2].description).toContain("ChatGPT subscription");
  });

  it("labels by the value's OWN lane, not the configured provider", () => {
    // The agent runs on Claude; a Codex favorite must not read "Claude subscription".
    expect(modelCardHint("openai-codex:gpt-5.6-sol", "", "anthropic-oauth")).toContain("ChatGPT subscription");
    expect(modelCardHint("gateway:protolabs/coder", "", "anthropic-oauth")).toContain("Gateway");
  });

  it("still marks the configured default when the favorite is qualified", () => {
    expect(modelCardHint("gateway:protolabs/fast", "protolabs/fast")).toContain("configured default");
  });
});

describe("no favorites pinned", () => {
  it("falls back to every lane rather than just the configured one", () => {
    const data = modelPickerData(crossGroups([], LANES));
    const { choices, fromFavorites } = modelChoices(data);

    expect(fromFavorites).toBe(false);
    expect(choices).toEqual(LANES);
    expect(modelFormPayload(data, "").description).toContain("every model you're signed in to");
  });

  it("degrades to the configured lane's list when there is no cross-provider list", () => {
    // Older backend, or every lane probe failed — the picker must still work.
    const data = modelPickerData(groups([]));

    expect(modelChoices(data).choices).toEqual(["protolabs/reasoning", "protolabs/fast"]);
  });
});

describe("typed /model <alias>", () => {
  it("resolves a bare id to its qualified value", () => {
    const data = modelPickerData(crossGroups(LANES, LANES));

    expect(resolveModelArg(data, "claude-sonnet-5")).toBe("anthropic-oauth:claude-sonnet-5");
    expect(resolveModelArg(data, "anthropic-oauth:claude-sonnet-5")).toBe("anthropic-oauth:claude-sonnet-5");
    expect(resolveModelArg(data, "nope")).toBeNull();
  });
});

describe("groupByLane — the menu's sections", () => {
  it("groups by account in arrival order, labelling each", () => {
    const groups = groupByLane([
      "gateway:protolabs/coder",
      "gateway:protolabs/fast",
      "anthropic-oauth:claude-sonnet-5",
      "openai-codex:gpt-5.6-sol",
    ]);

    expect(groups.map((g) => [g.label, g.items.length])).toEqual([
      ["Gateway", 2],
      ["Claude subscription", 1],
      ["ChatGPT subscription", 1],
    ]);
  });

  it("keeps a model in its own lane's group even when it arrives late", () => {
    // Favorites are operator-ordered, so lanes can interleave; a later gateway pick must
    // join the gateway section rather than opening a second one.
    const groups = groupByLane([
      "gateway:protolabs/coder",
      "anthropic-oauth:claude-sonnet-5",
      "gateway:protolabs/fast",
    ]);

    expect(groups.length).toBe(2);
    expect(groups[0].items).toEqual(["gateway:protolabs/coder", "gateway:protolabs/fast"]);
  });

  it("puts unqualified names in one unlabelled group — a flat menu, as before", () => {
    const groups = groupByLane(["protolabs/reasoning", "protolabs/fast"]);

    expect(groups).toEqual([
      { lane: "", label: "", items: ["protolabs/reasoning", "protolabs/fast"] },
    ]);
  });
});
