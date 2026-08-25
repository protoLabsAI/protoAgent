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
// The lanes a value may name. The three in LANE_LABELS were the whole vocabulary when
// there were exactly three; an operator can now register `prod-gateway` or `local-vllm`.
// The set is DERIVED from the qualified options the server sent (their prefixes are
// registered ids by construction) rather than guessed from the string's shape — the
// backend only claims a prefix that names a registered connection, and a looser rule here
// would disagree with it, reading `bedrock:anthropic.claude` as a lane when the runtime
// treats it as a model name.
export function lanesFromOptions(options: string[]): Set<string> {
  const out = new Set(Object.keys(LANE_LABELS));
  for (const opt of options) {
    const i = (opt || "").indexOf(":");
    if (i <= 0 || i >= opt.length - 1) continue;
    const head = opt.slice(0, i).trim().toLowerCase();
    // Pass SERVER-BUILT option lists only. Their prefixes are registered connection ids
    // by construction, which is what keeps this agreeing with the backend — it claims a
    // prefix only when it names a registered connection. Feeding arbitrary stored values
    // in would register `bedrock` from a stored `bedrock:anthropic.claude` and display it
    // as `anthropic.claude`, which the runtime would never do. The id-shape check is a
    // second line: a gateway alias contains a slash and can never qualify.
    if (/^[a-z0-9][a-z0-9_-]*$/.test(head)) out.add(head);
  }
  return out;
}

export function laneOf(value: string, known?: Set<string>): string {
  const i = value.indexOf(":");
  if (i <= 0 || i === value.length - 1) return "";
  const head = value.slice(0, i).trim().toLowerCase();
  const lanes = known ?? new Set(Object.keys(LANE_LABELS));
  return lanes.has(head) ? head : "";
}

export function laneLabel(lane: string): string {
  // A registered connection the console has no canned name for is shown by its id — the
  // operator chose it, so it is already the most meaningful label available here.
  return LANE_LABELS[lane] ?? lane;
}

/** The model id without its lane prefix — what a card should actually be titled. */
export function bareModel(value: string, known?: Set<string>): string {
  const lane = laneOf(value, known);
  return lane ? value.slice(lane.length + 1).trim() : value;
}

const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && !!x) : [];

/** Extract the picker's inputs from the settings schema (`GET /api/settings/schema`) —
 *  the SAME source the composer's model menu reads, so /model can never disagree with it. */
export function modelPickerData(groups: SettingsGroup[], liveProvider = ""): ModelPickerData {
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
    // `model.provider` is no longer RENDERED (ADR 0106 — Connections owns it), so reading
    // it from the schema now yields "" and the subscription labels below would silently
    // degrade to "gateway model". The primary model names its connection, so its lane is
    // the better answer; the retired field stays as a fallback for an older backend that
    // still renders it. Both disappear with the field itself.
    // `model.provider` is `ui_hidden` (ADR 0106), so the schema no longer carries it and
    // reading it alone made every subscription card on a legacy config read "gateway
    // model". Order: the schema if an older backend still renders it, then the lane the
    // primary model names, then the RUNTIME's live provider — which is the only source
    // left once the field is gone, and the one the console already has.
    provider:
      (typeof prov?.value === "string" ? prov.value.trim().toLowerCase() : "") ||
      laneOf(globalModel, lanesFromOptions(models)) ||
      (liveProvider || "").trim().toLowerCase(),
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
  // The choices themselves say which lanes exist — no second source to fall out of sync.
  const known = lanesFromOptions(choices);
  for (const choice of choices) {
    const lane = laneOf(choice, known);
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
/** Do these two model values select the same model, whichever way each is written?
 *
 *  "Is this the configured default?" is asked wherever a model is listed, and both sides
 *  can now be qualified or bare: the primary model names its connection since ADR 0106,
 *  while a favorite or a stored slot may still be bare — or the reverse. Comparing the
 *  raw strings made the marker vanish whenever the two spellings disagreed, which is the
 *  same defect twice: once when favorites were pinned qualified against a bare
 *  `model.name`, and again when `model.name` itself became qualified.
 *
 *  Qualified-vs-qualified compares whole (a model on two connections is two choices);
 *  otherwise the bare ids decide. */
export function sameModel(a: string, b: string, known?: Set<string>): boolean {
  const x = (a || "").trim();
  const y = (b || "").trim();
  if (!x || !y) return false;
  if (x === y) return true;
  const lx = laneOf(x, known);
  const ly = laneOf(y, known);
  if (lx && ly) return false; // both name a connection and they differ
  return bareModel(x, known) === bareModel(y, known);
}

export function modelCardHint(alias: string, globalModel: string, provider = "", known?: Set<string>): string {
  // A qualified value names its own lane, so that wins over everything: the whole point
  // is that this card may run on a DIFFERENT provider than the agent is configured for.
  const lane = laneOf(alias, known);
  const source = lane
    ? laneLabel(lane)
    : providerOf(alias) || SUBSCRIPTION_LABELS[provider] || "gateway model";
  const parts = [source];
  if (sameModel(alias, globalModel, known)) parts.push("configured default");
  return parts.join(" · ");
}

/** The one-step card-form payload (same shape as /effort's picker). `current` — the tab's
 *  effective model — preselects its card when it's among the choices. */
export function modelFormPayload(data: ModelPickerData, current: string): HitlPayload {
  const { choices, fromFavorites } = modelChoices(data);
  // Derived from the very choices these cards list, so the title, the hint and the
  // "configured default" marker can never disagree about what counts as a lane.
  // `choices` may be the operator's favorites, which can be stored bare; `data.models`
  // and `data.crossProvider` are the schema's qualified option lists, so they carry the
  // lane names authoritatively.
  const knownLanes = lanesFromOptions([...data.models, ...data.crossProvider, ...choices]);
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
              // Pre-select on MEANING, not spelling: the configured model may be written
              // `prod-gateway:protolabs/reasoning` while the favorite listing it is bare
              // (or the reverse). An exact `includes` left the picker with nothing
              // selected the moment the two spellings diverged.
              ...(() => {
                const match = choices.find((c) => sameModel(c, current, knownLanes));
                return match ? { default: match } : {};
              })(),
              // `const` keeps the QUALIFIED value (that's what gets applied and saved);
              // the title shows the bare id, with the lane carried in the hint — a card
              // reading "anthropic-oauth:claude-sonnet-5" is noise, not information.
              oneOf: choices.map((m) => ({
                const: m,
                title: bareModel(m, knownLanes),
                description: modelCardHint(m, data.globalModel, data.provider, knownLanes),
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
  // The lane set has to reach the bare-name comparison too. Without it `bareModel` only
  // knew the three legacy lanes, so `/model qwen3-32b` could not find
  // `local-vllm:qwen3-32b` — the fallback silently stopped working for exactly the custom
  // connections this PR makes possible. Derived from the server-built lists, as everywhere.
  const lanes = lanesFromOptions([...data.models, ...data.crossProvider]);
  // Exact match first (someone may type the qualified form), then the bare id — typing
  // `/model claude-sonnet-5` should find `anthropic-oauth:claude-sonnet-5` rather than
  // reporting it unknown.
  return (
    known.find((m) => m.toLowerCase() === t) ??
    known.find((m) => bareModel(m, lanes).toLowerCase() === t) ??
    null
  );
}
