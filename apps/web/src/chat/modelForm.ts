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
  /** The provider's full model list (model.name options). */
  models: string[];
  /** The configured default model (model.name value) — picking it clears the tab override. */
  globalModel: string;
  /** Every lane's models, lane-qualified — the no-favorites fallback, so an operator
   *  with no favorites still sees Claude and Codex, not just the configured lane. */
  crossProvider: string[];
  /** The configured model.provider — native OAuth providers relabel the cards (#2473). */
  provider: string;
};

/** Native OAuth subscription providers (ADR 0097) → the card/source label their models
 *  carry. A subscription model is NOT a "gateway model" — that wording made the picker
 *  look wrong the moment discovery worked (#2473). */
const SUBSCRIPTION_LABELS: Record<string, string> = {
  "openai-codex": "ChatGPT subscription",
  "anthropic-oauth": "Claude subscription",
};

/** Lane labels for a QUALIFIED slot value (`<provider>:<model>`, part A). Distinct from
 *  SUBSCRIPTION_LABELS above, which describes the *configured* provider — a qualified
 *  value names its own lane and must be labelled by that, not by what the agent
 *  happens to be running on. */
const LANE_LABELS: Record<string, string> = {
  gateway: "Gateway",
  "openai-codex": "ChatGPT subscription",
  "anthropic-oauth": "Claude subscription",
};

/** The lane a qualified value names ("anthropic-oauth:claude-sonnet-5" → that provider);
 *  "" for an unqualified name. Mirrors `split_slot_target` on the backend: only a KNOWN
 *  lane counts, so "openai/gpt-5" stays a gateway alias and never reads as a lane. */
export function laneOf(value: string): string {
  const i = value.indexOf(":");
  if (i <= 0) return "";
  const head = value.slice(0, i).trim().toLowerCase();
  return head in LANE_LABELS ? head : "";
}

/** The human label for a lane id — "" for an unknown one, so a caller can just test it. */
export function laneLabel(lane: string): string {
  return LANE_LABELS[lane] ?? "";
}

/** The model id without its lane prefix — what a card should actually be titled. */
export function bareModel(value: string): string {
  const lane = laneOf(value);
  return lane ? value.slice(lane.length + 1).trim() : value;
}

const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && !!x) : [];

/** Extract the picker's inputs from the settings schema (`GET /api/settings/schema`) —
 *  the SAME source the composer's model menu reads, so /model can never disagree with it. */
export function modelPickerData(groups: SettingsGroup[]): ModelPickerData {
  const fields = groups.flatMap((g) => g.fields);
  const name = fields.find((f) => f.key === "model.name");
  const favs = fields.find((f) => f.key === "model.favorites");
  const prov = fields.find((f) => f.key === "model.provider");
  const globalModel = typeof name?.value === "string" ? name.value : "";
  const models = strings(name?.options);
  return {
    favorites: [...new Set(strings(favs?.value))],
    models: models.length ? models : globalModel ? [globalModel] : [],
    // The favorites field's OPTIONS are the cross-provider, lane-qualified list (part B's
    // `slot_models`). Reusing it means the picker spans every signed-in provider without
    // a second request — the schema already carries the answer.
    crossProvider: [...new Set(strings(favs?.options))],
    globalModel,
    provider: typeof prov?.value === "string" ? prov.value.trim().toLowerCase() : "",
  };
}

/** The provider segment of a gateway alias ("openai/gpt-5.2" → "openai"); "" when the
 *  alias has no provider prefix. */
export function providerOf(alias: string): string {
  const i = alias.indexOf("/");
  return i > 0 ? alias.slice(0, i) : "";
}

export type ModelLaneGroup = { lane: string; label: string; items: string[] };

/** Group picker choices by lane, preserving the order they arrived in.
 *
 * Unqualified names lead in one unlabelled group — a single-lane operator (or an older
 * backend) sees a flat list, exactly as before, because a lone "Gateway" heading over
 * every row is chrome, not information. */
export function groupByLane(choices: string[]): ModelLaneGroup[] {
  const groups: ModelLaneGroup[] = [];
  for (const choice of choices) {
    const lane = laneOf(choice);
    const last = groups.find((g) => g.lane === lane);
    if (last) last.items.push(choice);
    else groups.push({ lane, label: laneLabel(lane), items: [choice] });
  }
  return groups;
}

/** The cards /model offers: the favorites when any are pinned, else every lane's models
 *  (falling back to the configured lane's list when the cross-provider one is empty —
 *  an older backend, or a probe that failed). */
export function modelChoices(data: ModelPickerData): { choices: string[]; fromFavorites: boolean } {
  if (data.favorites.length) return { choices: data.favorites, fromFavorites: true };
  return { choices: data.crossProvider.length ? data.crossProvider : data.models, fromFavorites: false };
}

/** One-line card hint: the model's source (alias prefix, subscription label, or
 *  "gateway model") plus a "configured default" marker. */
export function modelCardHint(alias: string, globalModel: string, provider = ""): string {
  // A qualified value names its own lane, so that wins over everything: the whole point
  // is that this card may run on a DIFFERENT provider than the agent is configured for.
  const lane = laneOf(alias);
  const source = lane
    ? LANE_LABELS[lane]
    : providerOf(alias) || SUBSCRIPTION_LABELS[provider] || "gateway model";
  const parts = [source];
  // Compare on the bare id too: a favorite pinned as `gateway:protolabs/fast` IS the
  // configured default when model.name is `protolabs/fast`.
  if (alias === globalModel || (lane && bareModel(alias) === globalModel)) parts.push("configured default");
  return parts.join(" · ");
}

/** The one-step card-form payload (same shape as /effort's picker). `current` — the tab's
 *  effective model — preselects its card when it's among the choices. */
export function modelFormPayload(data: ModelPickerData, current: string): HitlPayload {
  const { choices, fromFavorites } = modelChoices(data);
  const sourceLabel = data.crossProvider.length
    ? "every model you're signed in to"
    : SUBSCRIPTION_LABELS[data.provider]
      ? `every model your ${SUBSCRIPTION_LABELS[data.provider]} offers`
      : "every gateway model";
  return {
    kind: "form",
    title: "Switch model",
    description: fromFavorites
      ? "Applies to this tab's next message. Manage favorites in Settings ▸ Model."
      : `No favorites pinned yet — showing ${sourceLabel}. Pin favorites in Settings ▸ Model ▸ Favorite models to shorten this list.`,
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
              // `const` keeps the QUALIFIED value (that's what gets applied and saved);
              // the title shows the bare id, with the lane carried in the hint — a card
              // reading "anthropic-oauth:claude-sonnet-5" is noise, not information.
              oneOf: choices.map((m) => ({
                const: m,
                title: bareModel(m),
                description: modelCardHint(m, data.globalModel, data.provider),
              })),
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
  const known = [...data.favorites, ...data.models, ...data.crossProvider];
  if (!known.length) return arg.trim();
  // Exact match first (someone may type the qualified form), then the bare id — typing
  // `/model claude-sonnet-5` should find `anthropic-oauth:claude-sonnet-5` rather than
  // reporting it unknown.
  return (
    known.find((m) => m.toLowerCase() === t) ?? known.find((m) => bareModel(m).toLowerCase() === t) ?? null
  );
}
