# 0080 — External secrets manager: env hydration with pluggable providers (Infisical first)

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** Josh Mabry
- **Tags:** secrets, config, security, extensibility
- **Related:** ADR 0019 (plugin config/secrets), ADR 0047 (settings cascade), ADR 0004/0065
  (instance scoping), ADR 0071 (plugin trust model), ADR 0074 (lifecycle events)

## 1. Context & problem statement

protoAgent's secret precedence is `secrets.yaml` → main YAML → env var
(`docs/reference/configuration.md`), and the env tier exists *specifically* so
env-injected deployments (`infisical run -- python -m server`, `entrypoint.sh`) work with
no code support. That wrapper story has real limits for a long-running, self-hosted agent
server:

- **Rotation requires a restart** — the wrapper snapshots secrets at exec. Infisical's
  `--watch` restarts the whole child, which is disruptive for a stateful server with
  in-flight runs and SSE streams (and its docs discourage it in production).
- **It fights our launchers** — systemd units, `scripts/dev.sh`, the desktop (Tauri)
  sidecar, and frozen builds can't reasonably be re-parented under a third-party binary,
  and the fleet supervisor spawns members itself.
- **No console UX** — nothing to configure, test, or observe from Settings.

Prior art informing this design: hermes-agent's `SecretSource` orchestrator (PR #59498) —
whose fetch/apply split, never-raise provider contract, protected bootstrap vars, and
provenance tracking we adopt, and whose known weaknesses (startup-only fetch, plaintext
disk cache, plugin-provider boot-ordering wart) we deliberately avoid — and the External
Secrets Operator's store-config + refresh-interval model.

**Ordering constraint (the design's spine):** config + secrets resolve once per load in
`LangGraphConfig.from_yaml` (`server/agent_init.py` boots through it), and plugins load
*after* that (ADR 0019 requires plugin config/secrets known before `register()`). Anything
that feeds secrets to the gateway key, `requires_env` gates, or plugin config must
therefore run **before** the config parse — which rules out a plugin-provided backend
without new pre-config machinery.

## 2. Decision

**D1 — Env hydration, not a new secret store.** A configured external manager is fetched
and its secrets are written into `os.environ` immediately before the config parse, on
every real load path (`from_yaml` — boot, `--setup`, config hot-reload, CLIs, fleet
members). Pre-existing env vars win (`setdefault` semantics) unless
`secrets_manager.override_env: true`. This adds **zero new precedence semantics**: the
manager simply populates the env tier that `secrets.yaml → YAML → env` already documents,
so the gateway key (`model.api_key` env fallback), `A2A_AUTH_TOKEN`, plugin
`requires_env` load gates, MCP/delegate child env, and tools that read env all pick up
manager values with no changes.

**D2 — Provider seam in `infra/secrets/`.** A `SecretsProvider` contract (fetch-only:
`fetch(SourceConfig) -> FetchResult`; **never raises, never prompts**; typed `ErrorKind`)
plus a registry (`register_secrets_provider`). The **orchestrator** (`hydrate.py`) owns
everything dangerous: env-name validation, protected bootstrap vars, existing-env
precedence, ownership/provenance tracking, removals, redaction registration, and all
`os.environ` writes — no provider can get policy wrong. Layering: `infra/` is importable
from `graph/` (the config loader calls it) and from `server`/`operator_api` (refresh loop,
routes); it imports neither.

**D3 — Infisical ships built-in, over raw REST.** Universal-auth machine identity:
`POST /api/v1/auth/universal-auth/login` → bearer token (re-login at ~80% of `expiresIn`
or on 401), then `GET /api/v3/secrets/raw` (`workspaceId`/`environment`/`secretPath`/
`recursive`/`expandSecretReferences`/`include_imports`). The v3-raw endpoint is chosen
deliberately — it is what the official SDK and External Secrets Operator call, so it works
against old and new self-hosted servers alike. No SDK dependency: the current
`infisicalsdk` unconditionally drags boto3+botocore and has no token renewal; two httpx
calls are smaller than the dependency (httpx is already a core dep, which also keeps
frozen desktop builds working).

**D4 — Bootstrap credentials stay local, first-class in the existing secrets machinery.**
The machine-identity `client_id`/`client_secret` are the one secret that cannot come from
the manager. They resolve `secrets.yaml` (`secrets_manager.client_id/client_secret`) →
main YAML → env (`INFISICAL_CLIENT_ID`/`INFISICAL_CLIENT_SECRET`) — the standard secret
precedence. Both keys are added to `SECRET_PATHS` and exposed as `type: secret` settings
fields, so the console stores them in `secrets.yaml` and never echoes them back, exactly
like `model.api_key`. The recommended posture becomes: *secrets.yaml holds only the
machine identity; everything else lives in the manager.* Fetched values can never
overwrite the bootstrap vars (protected set), nor `PROTOAGENT_*` instance identity.

**D5 — Refresh is designed in, not bolted on.** A self-guarding server loop re-fetches
every `refresh_seconds` (0 = boot/reload only) and re-applies: only vars the orchestrator
*owns* (provenance) are ever updated or removed, so a rotation lands without touching
user-set env. The lazily-read gateway key picks up rotation immediately; config-frozen
consumers follow on the next config reload. Operator surface: `GET /api/secrets/status`,
`POST /api/secrets/sync`, `POST /api/secrets/test` (fetch-only connection test) — the
console Settings panel drives these.

**D6 — Fail open by default, closed by choice.** A fetch failure logs one warning and the
boot continues with whatever the env already has (`ErrorKind` taxonomy in the status
surface). `secrets_manager.required: true` inverts this: `from_yaml` raises
(`SecretsRequiredError`) and boot fails fast rather than serving a half-configured agent.
In-process TTL cache only — **no plaintext disk cache** (hermes's disk cache produced a
follow-up chain of agent-access guards). Escape hatch: `PROTOAGENT_NO_SECRETS_HYDRATE=1`
disables hydration entirely for debugging.

**D7 — Redaction learns the fetched values.** Every applied value (≥ 8 chars) is
registered in a sensitive-values set that `graph/middleware/redaction.py` consults, so
manager-sourced secrets are scrubbed from audit logs and Langfuse spans by exact match —
on top of the existing pattern/key heuristics. Secret *values* never appear in logs or the
status API; var *names* appear only in the operator-authed status surface, not in logs.

### Non-goals / invariants

- **No plugin-provided providers (yet).** ADR 0019's ordering invariant means a plugin
  cannot feed core secrets without a pre-config discovery seam. The registry is the
  extension point; if a second provider materializes, it lands in `infra/secrets/` (~50
  lines) or waits for a manifest-declared pre-config seam designed on its own merits.
- **Single source in v1.** One `secrets_manager` section (flat, settings-schema-driven).
  Multi-source/multi-project lands later as an additive list without breaking this shape.
- **`infisical run` stays supported** — documented as the ops-side alternative; deployments
  using it are unaffected (they simply don't set `secrets_manager.enabled`).
- **Instance-scoped** (ADR 0004/0065): each instance's config names its own
  project/environment — the dev sandbox can pull `dev` while prod pulls `prod`.
- Stdio MCP children keep their default secret-filtered env; hydrated `*_KEY`/`*_TOKEN`
  vars do not leak to them unless the server config opts in (`inherit_env`).

## 3. Consequences

- Operators configure Infisical once in Settings (or YAML), keep two bootstrap env
  vars/secrets.yaml keys locally, and every other credential — gateway key, A2A token,
  plugin tokens — moves to the manager with rotation-without-restart.
- `from_yaml` may now perform network I/O **iff** `secrets_manager.enabled` — bounded by
  `timeout_seconds` (default 10 s), TTL-deduped across back-to-back loads, and inert when
  the section is absent (tests, unconfigured installs, forks unaffected).
- The refresh loop makes env mutation an ongoing behavior rather than boot-only; the
  ownership set keeps it scoped to manager-sourced vars.
- New golden fields in `tests/test_config_roundtrip.py`; settings schema grows a
  "Secrets manager" section the console renders generically (dedicated status/test panel
  is the follow-up console PR).

## 4. Alternatives considered

- **`infisical run` wrapper only (status quo).** Zero code, but restart-only rotation, no
  UX, hostile to desktop/frozen/fleet launchers. Kept as documentation, not the feature.
- **Secrets-overlay provider** (manager values merged where `secrets.yaml` sits, above
  YAML). Native redaction/provenance, but requires a flat-KEY → `section.key` mapping
  layer and produces no real env vars — missing `requires_env`, MCP env, tools, and
  spawned children, which is most of the ask.
- **Reference syntax (`${infisical://…}`) resolved at config load.** Most precise and
  least exposure; deferred — it layers cleanly on this seam later (the orchestrator
  gains `resolve(ref)`) but is overkill for v1's "pull in env vars".
- **Official `infisicalsdk`.** Buys retry and five auth methods; costs boto3+botocore
  (frozen-build weight), still no token renewal, and pins us to its churn (v1.0.3 broke
  response models). Two REST calls are the smaller contract.
- **Plugin-provided backend via a new pre-config seam.** Structurally what hermes-agent
  ended at (`register_secret_source`), but their plugin sources demonstrably miss the
  first boot's env load — the exact trap ADR 0019 forbids. Deferred until a real second
  provider demands it.
