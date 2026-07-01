# 0068 — Developer flags & the Developer panel (gating pre-release functionality)

Status: **Proposed**

> Resolves #1506. Features pass through stages — internal-testing → opt-in preview → GA — but
> the repo has **no first-class way to gate a half-built feature**. Today it either ships
> prematurely or rots on a long-lived branch (merge risk, velocity drag). This adds a small,
> **local/static** feature-flag system (`off` · `dev` · `beta` · `on`) plus a Developer panel to
> view and toggle flags, deliberately built by **reusing** the existing config-schema, settings-IA,
> and device-local-state machinery rather than a parallel system. Explicit non-goals (per #1506):
> A/B experiments, percentage rollouts, and a remote flag service (LaunchDarkly-style).

## Context

- A feature that's functional-enough-for-internal-testing but not GA has nowhere to live. The two
  status-quo options both hurt: merge it dark and risk exposing an unfinished path, or hold it on a
  branch that drifts from `main`.
- protoAgent already has three adjacent mechanisms a flag system must **not** duplicate:
  - **Layered config** — the App→Host→Agent cascade (`graph/config.py:742-771`, deep-merge agent-wins
    `:768`) with the typed `Field`/`FIELDS` schema (`graph/settings_schema.py:23-51,61-687`) and the
    plugin-config precedence **ENV > UI > default** (`plugins/artifact/__init__.py:60-102`).
  - **The settings surface** — domain-first IA (ADR 0048): four section registries in
    `apps/web/src/settings/SettingsSurface.tsx`, incl. `CONSOLE_SECTIONS` (device-local prefs —
    Theme/Chat/Keyboard, **no cascade**, localStorage), and the schema-driven `SettingInput` renderer.
  - **Plugin enable/disable** — a **load-time** gate on whether a plugin's code/tools/router mount at
    all (`graph/plugins/loader.py:257-299`, `plugins.enabled`/`plugins.disabled` lists).
- There is **no existing** flag / experiment / preview / beta *gating* concept in the tree (greenfield);
  the `builtin` boolean on a plugin manifest is the only "flag"-named thing and is unrelated.
- Scope: **local, static** flags only. A flag is a temporary gate on a **core** code path, meant to be
  **deleted** when the feature graduates — not a permanent capability toggle (that's a plugin) and not
  user-facing configuration (that's a setting).

## Decision

### D1 — One backend registry is the single source of truth

Flags are declared **once**, in a core registry `runtime/flags.py`, as an ordered `list[Flag]` — the
same shape as the config `FIELDS` list, so the pattern is already familiar:

```python
@dataclass(frozen=True)
class Flag:
    id: str                     # "chat.new_dashboard" — dotted, stable; the override/lookup key
    description: str            # what it gates (shown in the Developer panel)
    tier: Tier = "off"          # rollout stage: "off" | "dev" | "beta" | "on"
    owner: str = ""            # who to ask; makes stale flags actionable
    remove_by: str | None = None  # a version or ISO date — the cleanup deadline (D6)

FLAGS: list[Flag] = [ ... ]
```

`flag_enabled(id) -> bool` resolves a flag for the current process (D3). The console reads the resolved
states + metadata from **`GET /api/flags`** (operator surface). A flag that gates only console UI still
registers here — the AC "*a flag is defined in a single place*" wins over saving a backend round-trip
(cheap: it's one metadata row). This mirrors settings exactly: the schema is backend, the console renders.

### D2 — Four tiers = the flag's rollout stage; a runtime **channel** is what it's measured against

- **`tier`** is the flag's declared maturity: `off` (kill switch — nobody), `dev` (developers only),
  `beta` (opt-in preview), `on` (GA — everybody).
- **`channel`** is the runtime's openness: `prod ⊂ beta ⊂ dev` (dev sees the most). A flag is enabled when
  the channel is open enough for its tier: `on`→all channels, `beta`→beta+dev, `dev`→dev only, `off`→never.
- **Channel is derived, not configured per-flag:**
  - the **dev sandbox instance** (`PROTOAGENT_INSTANCE=dev`, ADR 0065) defaults to `dev`;
  - a Vite dev build (`import.meta.env.DEV`) is `dev` on the frontend;
  - otherwise a single box/agent field **`developer.channel`** (`prod|beta|dev`, default `prod`, added to
    `FIELDS`) sets it — so an operator can opt a whole instance into `beta`.

  This means a developer on the dev instance auto-sees `dev`-tier features; production sees only `on`.

### D3 — Resolution precedence mirrors the existing config ladder

Most-explicit / most-immediate wins, exactly like `plugins/artifact`'s ENV > UI > default:

1. **Env override** — `PROTOAGENT_FLAG_<ID>=on|off` (headless / CI / deployment escape hatch).
2. **Query param** — `?flag:<id>=on|off` (a shareable "try this build" link; this-load only).
3. **Developer-panel toggle** — a per-session override persisted device-locally (D4).
4. **Channel-vs-tier** (D2) — the declared rollout stage.
5. **Off** — unknown/unset denies by default.

Env is checked first (deployment-level, matches the plugin-config precedence), then the transient/session
overrides, then the channel default.

### D4 — The Developer panel is a device-local settings section, gated to `dev`/`beta`

A new **Settings ▸ Developer** section (a `CONSOLE_SECTIONS` entry in `SettingsSurface.tsx` — the same
device-local, no-cascade home as Theme/Chat/Keyboard) lists every registered flag with its tier, resolved
state, a **per-session override** toggle, and **Reset**. Override state is device-local (localStorage via
the `createUISlice`/`uiStore` pattern, `apps/web/src/ext/uiStateRegistry.ts:39-47`) — a developer's toggles
don't leak into shared config. The section is **hidden unless `channel ≥ beta`** (or revealed by the
`?flag:` query param), so production users never see it. Backend also honors `PROTOAGENT_FLAG_*` for
headless/ACP where there's no panel.

### D5 — A flag check is a cheap in-memory lookup, never a per-render network call

Backend: `flag_enabled()` reads the in-process registry + resolved channel/overrides — a map lookup.
Frontend: `/api/flags` is fetched **once at boot** into a store; `useFlag("id")` and an imperative
`flagEnabled("id")` read from it (no fetch per render). Gating a feature is `if (useFlag("x")) …` /
`if flag_enabled("x"):` wrapped around the new path — no restructuring (satisfies the "wrap, don't
restructure" AC).

### D6 — Cleanup is a contract, not a hope

Graduating a flag to `on` and then **deleting the flag + the old code path in one PR** is the intended
end state (the AC). Each `Flag` carries `owner` + optional `remove_by`; a test (`tests/test_flags.py`)
**fails when a flag is past its `remove_by`**, so stale gates are visible debt rather than silent
accretion. A flag is a loan against future cleanup — the registry makes the loan book auditable.

### D7 — Why a flag is neither a plugin nor a setting (the overlap the reviewer will ask about)

| | gates | lifecycle | audience | where it lives |
|---|---|---|---|---|
| **plugin enable/disable** | whether code/tools/router **load** | permanent capability | operator | `plugins.enabled/disabled` |
| **setting** | **shipped** behavior's configuration | permanent | user | config cascade (`FIELDS`) |
| **developer flag** | a **pre-release** core code path, already loaded | **temporary — deleted at GA** | developer / internal | `runtime/flags.py` + overrides |

Conflating them is the failure mode: a half-built core feature has no manifest/sandbox (not a plugin),
and users shouldn't configure unfinished behavior as if it were permanent (not a setting).

### D8 — Scope of flag state follows where it's stored (reusing ADR 0004/0065)

The **channel** is per-instance (the dev instance is `dev`) or per-box (if `developer.channel` is made
`scope="host"`). Panel **overrides** are per-agent-slug per-browser (localStorage). Env overrides are
per-process. No new scoping machinery — it falls out of the existing instance model.

## Consequences

- A feature can merge to `main` early behind a `dev`/`beta` flag: exercised on the dev instance and by
  internal testers, invisible in prod, no long-lived branch. Velocity up, merge risk down.
- One new concept to maintain — bounded by D6 (the `remove_by` test) and D7 (a clear "is this really a
  flag?" test before adding one).
- Pure-frontend flags pay a small tax (a backend registry row + a boot fetch) to keep one source of
  truth and one panel. Accepted deliberately (see Alternatives).
- The Developer panel is a new operator-surface leak of internal state — mitigated by gating it to
  `channel ≥ beta` and by flags being descriptions of *unfinished* work, not secrets.

## Alternatives considered

- **Env-vars only, no registry/panel** — rejected: not discoverable, no tiers, no runtime toggle without
  a restart, and no cleanup accounting. (Env stays as the *headless override* layer, D3.)
- **Two registries (a separate frontend flag list for console-only flags)** — rejected as the default: it
  reintroduces the drift the single-source rule exists to prevent. A fork *may* add frontend-only flags
  via a `createUISlice`-style registry that merges into the panel, but core keeps one backend source.
- **Reuse plugin enable/disable for pre-release features** — rejected: enable/disable is a *load-time*
  gate for *permanent* capabilities with manifests/sandboxes (D7); a flag gates behavior inside
  already-loaded core code and is meant to be deleted.
- **Model flags as ordinary settings (`FIELDS`)** — rejected: settings are permanent, user-facing, and
  cascade; flags are temporary, developer-facing, and channel-resolved. Borrowing the *renderer* and
  *precedence pattern* is right; borrowing the *semantics* is not.
- **A remote flag service (LaunchDarkly-style)** — explicit non-goal (#1506): adds a network dependency
  and an external SaaS for what is, at this scale, a static in-repo list.
- **Percentage / cohort rollouts** — explicit non-goal; the four tiers cover the internal→beta→GA path
  we actually have.

## Slices

1. **Backend core** — `runtime/flags.py` (`Flag`, `FLAGS`, `flag_enabled`), channel derivation
   (instance/env), the `PROTOAGENT_FLAG_*` + `developer.channel` (`FIELDS`) overrides, unit tests.
2. **API** — `GET /api/flags` (resolved states + metadata) on the operator surface.
3. **Frontend runtime** — boot-load store + `useFlag`/`flagEnabled`, `?flag:<id>` query param, the
   localStorage session-override store, `import.meta.env.DEV`→`dev` channel.
4. **Developer panel** — the gated `CONSOLE_SECTIONS` section (list · tier · resolved state · toggle ·
   reset-all).
5. **Cleanup tooling** — `remove_by` + the `tests/test_flags.py` staleness guard.
6. **Dogfood** — put one real in-flight pre-release feature behind a flag and write the how-to guide
   (`docs/guides/`), proving the wrap-and-delete workflow end to end.
