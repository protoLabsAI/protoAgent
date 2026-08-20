# 0100 — Agent archetypes (starter agent types, ratified)

- Status: Accepted
- Date: 2026-08-14 (amended 2026-08-19 — **"stack" is retired**: archetype is the one
  product noun; a published bundle repo that ships an `archetype:` block is an
  **archetype repo**, named `<name>-archetype`. The former `*-stack` repos were renamed
  on GitHub (redirects keep old URLs and lock pins working). See Terminology.)
- Builds on: ADR 0040 (plugin bundles), ADR 0042 §F (the four lines that originally named
  archetypes), ADR 0083 (Cowork — the first bundle-backed archetype), ADR 0091 (snapshots,
  the second new-agent source), ADR 0094 (managed runtime — the `requires` warning).

## Context

The archetype system shipped incrementally through July 2026 — data-driven catalog
(#1565), preview endpoint (#2016), `tier` (#2241), `requires` (#2186), the
`requires_tools` capability contract (#2277/#2315), bundle-block `soul_preset` +
validation (#2715) — with its only written spec being four lines in ADR 0042 §F and a
narrative in `docs/guides/fleet.md`. The catalog and semantics have been stable since
2026-07-26. This ADR **ratifies the shipped design** so the next change has something to
amend; it introduces no new behavior.

## Decision

An **archetype** is a starter *agent type*: a persona plus, optionally, a bundle of
tools. It is data, never code.

### Two sources, one list

`GET /api/archetypes` merges, in order:

1. **The catalog** — `config/archetype-catalog.json`. Live config dir overrides the
   bundled seed (first dir that has the file wins, even if malformed — an operator's
   broken override must surface, not silently fall through). A hardcoded fallback
   (Basic + Custom) guarantees the picker never renders empty.
2. **Installed bundles** — every `plugins.lock` bundle row whose cached `archetype:`
   block has a `label` self-registers. Deduped against the catalog by id and by
   normalized bundle URL; catalog wins. Discovery is best-effort — a broken lock warns
   and never breaks the route.

`custom` is held back and appended **last**, after bundle self-registrations.

### Row schema

| field | catalog | bundle block | meaning |
|---|---|---|---|
| `id` | ✓ | bundle id | unique; the picker's radio value |
| `label`, `icon`, `blurb` | ✓ | ✓ (`label` required to register) | card copy; `icon` is a lucide name, default `Package` |
| `bundle` | ✓ (git URL or null) | the bundle's own URL | null = code-free persona |
| `soul` / `soul_preset` | ✓ | ✓ (#2715) | the base persona. `soul_preset` names a stem under `config/soul-presets/` (path-traversal-guarded; unknown → `""` + warning, console falls back to Basic's SOUL). Inline `soul` wins when both are set |
| `tier` | ✓ | ✓ | `standard` (default, inline card) or `advanced` (collapsed section) |
| `requires` | ✓ | ✓ | host capabilities needed to be USEFUL (e.g. `python_runtime`) — the picker warns at choose-time, never blocks |
| `requires_tools` | ✓ | ✓ | the capability contract (#2277): tool names the persona commits to using |

Unknown keys in a bundle's `archetype:` block warn at install and are otherwise ignored
(#2715). The block is cached into `plugins.lock` at bundle install so the picker never
re-fetches manifests.

### What picking an archetype does

Two consumers, two targets:

- **Setup wizard** (first run) targets **the host**: seeds the SOUL editor from the
  archetype's persona, and on finish installs its bundle into the host
  (`POST /api/plugins/install` — config defaults, `mcp:` servers, and declared secrets
  all seed, #1350/#2118), collecting the bundle's `${input}`s/secrets in a Configure
  step (#2714).
- **New-agent picker** (Settings ▸ Agents) targets a **new workspace**:
  `POST /api/fleet` → `manager.create` writes the persona to `<ws>/config/SOUL.md`,
  installs the bundle, enables its curated `enabled:` set, seeds config/mcp/secrets, and
  persists `requires_tools` to `workspace.yaml`; the whole create rolls back (`rmtree`)
  on any failure. At member boot, `capability_contract_warning()` compares the contract
  against the tools that actually bound and warns on gaps — the check is advisory,
  matching ADR 0071's trust posture.

**Preview before picking**: `GET /api/archetypes/{id}/preview` (`peek_bundle`,
TTL-cached) enumerates a bundle's members/mcp/secrets without installing anything;
`{bundle: null}` for code-free personas.

### Boundaries (what an archetype is NOT)

- **Not a fork.** Operator personas that would ship domain doctrine in the template stay
  downstream (ADR 0007) — the "operator-stack" proposal was deliberately superseded by
  core fleet telemetry (#2537 → #2539). Monitoring is core; personas are forks or
  archetype repos.
- **Not a team.** Declaring a whole fleet is ADR 0072 (team-via-config, Proposed) —
  archetypes are its per-member primitive. Note `blurb` is slated there (§B) to become
  the auto-derived delegate description; it currently has exactly one consumer (the
  picker card).
- **Not a snapshot.** ADR 0091 snapshots are the second new-agent source ("duplicate an
  existing agent"); archetypes are "start from a type". They share the picker surface,
  nothing else.

### Terminology

*(Amended 2026-08-19.)* Three words used to reference the same object from three axes —
archetype (the identity), bundle (the mechanism), stack (the repo) — and the third one
never needed a noun of its own. The delineation is now two concepts and a retiree:

- A **bundle** is the mechanism (ADR 0040: manifest, lock row, install/seed paths).
  It is HOW a pinned set of plugins travels; it has no identity. Code, API, and UI say
  `bundle`.
- An **archetype** is the product noun — WHO an agent starts as: a persona plus an
  optional bundle. It is the only term for "a prepackaged agent type" in the picker,
  wizard, docs, and marketing.
- A published bundle repo that ships an `archetype:` block is an **archetype repo**,
  named `<name>-archetype` (`cowork-archetype`, `social-archetype`, …). **"Stack" is
  retired** — it was product naming for exactly this repo shape and only blurred the
  model. The former `*-stack` repos were renamed; GitHub redirects keep old URLs and
  lock pins resolving.

The surface decides which face you touch: installing into an *existing* agent is
"installing the bundle"; creating a *new* agent is "starting from the archetype" —
same artifact underneath. A bundle with no `archetype:` block is just a bundle; an
archetype with no bundle (`basic`, `custom`) is a persona-only archetype.

## Consequences

- Adding a starter type is data: a catalog row (+ optional `soul_preset` file) in core,
  or an `archetype:` block in any bundle repo. No code change, no ADR — this document
  only needs amending when the *schema or semantics* move.
- The shipped catalog (2026-08): basic, cowork, social-marketing,
  project-manager (advanced), design-system (advanced), custom. There is **no** Lead
  Engineer archetype — PR #2178's subject was mislabeled; it shipped the
  project-manager row.
- Tests pinning this contract: `tests/test_fleet_routes.py` (merge/dedupe/preview/
  soul_preset), `tests/test_archetype_capability_contract.py`,
  `tests/test_bundled_config_assets.py` (per-row catalog integrity, custom-last,
  packaging), `apps/web` NewAgentPanel + e2e fleet specs.
