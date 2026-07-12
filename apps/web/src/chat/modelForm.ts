// Pure logic for the `/model` quick-switch picker (#1957) — kept out of
// coreSlashCommands.ts so it can be unit-tested without the registry side effects.
// The picker is a one-step HITL form whose single `model` field renders as option
// cards (the `oneOf` + descriptions turn it into cards — see hitl-form.isCardChoice),
// exactly the `/effort` pattern. Cards come from the operator's pinned favorites
// (Settings ▸ Model ▸ Favorite models); with none pinned it falls back to the
// gateway's full model list, with a hint to pin favorites.

import type { HitlPayload, SettingsGroup } from "../lib/types";

export type ModelPickerData = {
  /** Pinned favorites (model.favorites), in the operator's order, deduped. */
  favorites: string[];
  /** The gateway's full model list (model.name options). */
  models: string[];
  /** The configured default model (model.name value) — picking it clears the tab override. */
  globalModel: string;
};

const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && !!x) : [];

/** Extract the picker's inputs from the settings schema (`GET /api/settings/schema`) —
 *  the SAME source the composer's model menu reads, so /model can never disagree with it. */
export function modelPickerData(groups: SettingsGroup[]): ModelPickerData {
  const fields = groups.flatMap((g) => g.fields);
  const name = fields.find((f) => f.key === "model.name");
  const favs = fields.find((f) => f.key === "model.favorites");
  const globalModel = typeof name?.value === "string" ? name.value : "";
  const models = strings(name?.options);
  return {
    favorites: [...new Set(strings(favs?.value))],
    models: models.length ? models : globalModel ? [globalModel] : [],
    globalModel,
  };
}

/** The provider segment of a gateway alias ("openai/gpt-5.2" → "openai"); "" when the
 *  alias has no provider prefix. */
export function providerOf(alias: string): string {
  const i = alias.indexOf("/");
  return i > 0 ? alias.slice(0, i) : "";
}

/** The cards /model offers: the favorites when any are pinned, else the full list. */
export function modelChoices(data: ModelPickerData): { choices: string[]; fromFavorites: boolean } {
  return data.favorites.length
    ? { choices: data.favorites, fromFavorites: true }
    : { choices: data.models, fromFavorites: false };
}

/** One-line card hint: the alias's provider plus a "configured default" marker. */
export function modelCardHint(alias: string, globalModel: string): string {
  const parts = [providerOf(alias) || "gateway model"];
  if (alias === globalModel) parts.push("configured default");
  return parts.join(" · ");
}

/** The one-step card-form payload (same shape as /effort's picker). `current` — the tab's
 *  effective model — preselects its card when it's among the choices. */
export function modelFormPayload(data: ModelPickerData, current: string): HitlPayload {
  const { choices, fromFavorites } = modelChoices(data);
  return {
    kind: "form",
    title: "Switch model",
    description: fromFavorites
      ? "Applies to this tab's next message. Manage favorites in Settings ▸ Model."
      : "No favorites pinned yet — showing every gateway model. Pin favorites in Settings ▸ Model ▸ Favorite models to shorten this list.",
    steps: [
      {
        schema: {
          type: "object",
          required: ["model"],
          properties: {
            model: {
              type: "string",
              title: "Model",
              ...(choices.includes(current) ? { default: current } : {}),
              oneOf: choices.map((m) => ({ const: m, title: m, description: modelCardHint(m, data.globalModel) })),
            },
          },
        },
      },
    ],
  };
}

/** Resolve a typed `/model <alias>` argument to its canonical alias (case-insensitive,
 *  against favorites ∪ the full list), or null when unknown. With NO known models at all
 *  (gateway list unavailable) the typed alias is trusted as-is. */
export function resolveModelArg(data: ModelPickerData, arg: string): string | null {
  const t = arg.trim().toLowerCase();
  if (!t) return null;
  const known = [...data.favorites, ...data.models];
  if (!known.length) return arg.trim();
  return known.find((m) => m.toLowerCase() === t) ?? null;
}
