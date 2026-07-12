import { describe, expect, it } from "vitest";

import type { SettingsGroup } from "../lib/types";
import {
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
