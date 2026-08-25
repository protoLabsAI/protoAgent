# 0106 — Providers are a registry; every model reference names its provider

Status: **Accepted** (supersedes the single-lead-provider half of ADR 0097)

## Context

Model configuration had exactly one provider identity:

```yaml
model:
  provider: openai            # openai | anthropic-oauth | openai-codex
  name: protolabs/reasoning   # bare — means "in whatever provider is"
  api_base: https://…/v1      # THE gateway
  api_key: …                  # THE gateway's key
```

Every other model slot (`aux_model`, `compaction.model`, `goal.eval_model`,
subagent models) either inherited that provider or carried a qualified
`<provider>:<model>` name (#2574) drawn from a hardcoded triple —
`graph/llm.py: _SLOT_PROVIDERS = ("gateway", "anthropic-oauth", "openai-codex")`.

Three consequences, all reported by the operator rather than found in review:

- **"Isn't the current default" is nonsense to a user.** `OAuthAccountSection`
  rendered *"Your Claude subscription is connected but isn't the current
  default"* for every connected account except one. #3097 made all connected
  accounts *visible*; it could not make them *equal*, because the config has a
  lead provider by construction.
- **Two OpenAI-compatible gateways are unrepresentable.** `gateway` is a single
  lane backed by a single `api_base`/`api_key` pair. A production gateway plus a
  local vLLM, or two gateways with different key scopes, cannot both exist.
- **A pile of machinery exists only to defend the singleton.**
  `_MODEL_IDENTITY_KEYS`, `_drop_host_model_identity`, `_reconcile_slot_providers`,
  `_native_provider_without_gateway`, `_is_gateway_alias` — roughly 150 lines of
  `graph/config.py` whose entire job is keeping other slots coherent with the lead
  provider. Their most expensive failure (fleet members inheriting
  `anthropic-oauth` on top of a gateway alias and crash-looping at boot) is a
  direct product of name and provider being separable in the first place.

## Decision

**Providers become a registry of connections. Every model reference is qualified.**

```yaml
providers:
  - id: gateway               # slug, operator-chosen, IMMUTABLE
    type: openai-compat
    label: Production gateway # display only, freely editable
    base_url: https://api.proto-labs.ai/v1
    api_key: …                # overlaid from secrets.yaml, like model.api_key was
  - id: local-vllm
    type: openai-compat
    base_url: http://localhost:8000/v1
  - id: claude
    type: anthropic-oauth     # credential lifecycle unchanged (ADR 0097)

model:
  name: gateway:protolabs/reasoning   # qualified, always
```

`type` is the *kind* of connection (`openai-compat`, `anthropic-oauth`,
`openai-codex`); `id` is *which one*. Multiple entries may share a type — that is
the entire point, and what makes several gateways possible.

### The grammar is unchanged, its whitelist is not

`split_slot_target` already parses `<prefix>:<model>` and already refuses to claim
a prefix that isn't a known provider — which is what keeps `bedrock:anthropic.claude`
a model name. The only change is that the whitelist becomes **the registered
provider ids** instead of a hardcoded tuple. Provider ids are therefore constrained
to `[a-z0-9][a-z0-9_-]*` (no colon, no slash), so the grammar stays unambiguous by
construction.

### Migration is an identity function

The three legacy lane names are kept as the three default ids: a config with no
`providers:` key synthesizes `gateway` from `model.api_base`/`api_key`, and
`anthropic-oauth` / `openai-codex` entries when a credential store exists for them.
**Every already-stored `gateway:…` / `anthropic-oauth:…` / `openai-codex:…` slot
value therefore keeps resolving to the same place**, and a bare `model.name` is
qualified with the legacy `model.provider`'s lane. No operator action, no rewrite of
fleet member configs.

### Ids are immutable; labels are not

An id appears inside stored model values, and via the fleet host layer those values
can live in *another instance's* config, which a rename cannot reach. So an id is
chosen once at creation and frozen; renaming is remove-and-re-add. The `label` is
display-only and freely editable, which is where the ergonomics live.

### The legacy fields become derived, deprecated aliases

`config.model_provider`, `config.api_base` and `config.api_key` are read by forks,
plugins, snapshot import/export and the fleet host layer. They remain as read-only
properties derived from the primary model's provider, marked deprecated, and are
scheduled for removal no earlier than **v0.152.0**. Nothing in core reads them.

### What is deleted, not adapted

`_reconcile_slot_providers`, `_native_provider_without_gateway`, `_is_gateway_alias`,
`_MODEL_IDENTITY_KEYS` and `_drop_host_model_identity` are removed. A slot cannot be
incoherent with the lead provider when there is no lead provider: a qualified name
either names a registered provider or is a plain model id. The fleet crash-loop these
defended against becomes unreachable rather than defended.

## Consequences

- Settings ▸ Model's account section becomes **Settings ▸ Providers** — a list you
  add to, test and remove from, with no "active" state. The generic flat-key settings
  schema cannot express a list of objects, so this gets its own panel and routes, as
  Delegates and Plugins already do.
- Model pickers group by provider, reusing the heading + `MenuSeparator` convention
  chosen in #2581: rows show bare model names, the heading carries the account.
- **Visible change:** dropdowns and stored values read `gateway:protolabs/reasoning`
  rather than `protolabs/reasoning`, including for single-provider operators. This was
  already true of slot pickers (#2580); it now extends to the primary model. The
  qualified form is the stored value; the UI never asks anyone to type it.
- A provider whose credential is missing or expired is listed with `configured: false`
  and a reason, never omitted — "sign in to use Claude" beats silence (the #2580 rule,
  now applying to every entry rather than three fixed lanes).
- Adding a raw-API-key Anthropic or OpenAI connection (no gateway, no OAuth) is a new
  `type` and nothing else. Explicitly out of scope here, deliberately cheap later.

## Related

ADR 0097 (native OAuth providers — credential lifecycle unchanged), ADR 0047
(host-scoped model config), ADR 0089 (intra-instance trust), #2574/#2580/#2581 (the
qualified grammar and its pickers), #3097 (listing every connected account), #3104
(provider/model coherence at load — its check is subsumed by qualification).
