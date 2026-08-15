# Bundles — install, update, and publish plugin sets

You want several plugins that work *together* — a board tool plus a browser plus the
delegate spine — installed, updated, and removed as **one** thing, instead of
hand-assembling URLs, refs, and `plugins.enabled`. That's a **bundle**
([ADR 0040](/adr/0040-plugin-bundles)): a repo whose `protoagent.bundle.yaml` *names*
a pinned set of plugin repos plus everything needed to make them useful on arrival.

A published bundle that ships an `archetype:` block is called a **stack**
(`cowork-stack`, `social-stack`, …) — that's product naming for the starter catalog;
the mechanism is always "bundle" ([ADR 0100](/adr/0100-agent-archetypes)).

## The manifest

The full annotated reference lives at
[`examples/bundles/template/protoagent.bundle.yaml`](https://github.com/protoLabsAI/protoAgent/blob/main/examples/bundles/template/protoagent.bundle.yaml).
The shape:

```yaml
id: my-stack
name: My Stack
description: One line on what the set does together.
verified_against: 0.135.0        # core version the pins were last verified on (ADR 0049)
plugins:
  - { id: delegates, builtin: true }                       # ships with protoAgent — no fetch
  - { id: my_board,  url: https://github.com/o/board-plugin,  ref: v0.3.0 }
  - { id: my_view,   url: https://github.com/o/view-plugin,   ref: v0.1.2 }
enabled: [delegates, my_board]   # curated turn-on subset (empty = all members)
config:                          # per-plugin DEFAULTS — operator values always win
  my_board: { columns: 4 }
mcp:                             # MCP servers to seed, catalog-shaped (#2011)
  - template: { name: github, transport: http, url: "https://api.githubcopilot.com/mcp/",
                headers: { Authorization: "Bearer ${token}" } }
    inputs:
      - { key: token, env: GITHUB_MCP_TOKEN, required: true, secret: true }
secrets:                         # standalone secrets to prompt for / seed (#2041)
  - { key: acme_api_key, label: "Acme API key", secret: true }
archetype:                       # optional: appear in the new-agent picker (ADR 0100)
  label: My Stack
  icon: Boxes
  blurb: One-line pitch on the archetype card.
  soul_preset: my-stack          # or inline `soul:` markdown
```

Every member is installed exactly as a direct install would be — allowlist-checked and
SHA-pinned in `plugins.lock` — and the bundle itself is recorded in the lock's
`bundles:` section (that row powers provenance chips, the update check, and uninstall).
Unknown `archetype:` keys warn at install rather than vanishing.

## Install one

Where you install from decides what happens (ADR 0040, as amended):

- **Console** (Settings ▸ Plugins ▸ install by URL, the setup wizard's archetype pick,
  or Settings ▸ Agents ▸ new agent) — **installs, enables the curated set, seeds
  `config:`/`mcp:`/`secrets:`, and hot-reloads.** Installing is the consent
  (trust-by-default, [ADR 0071](/adr/0071-plugin-permissions-trust-model)). The wizard
  and new-agent picker collect the declared `${input}`s and secrets in a Configure
  step first; skipping falls back to the environment.
- **CLI** — fetch-only, never enables:

```sh
python -m server plugin install https://github.com/protoLabsAI/cowork-stack
# → members pinned in plugins.lock; enable list + config printed as suggestions
```

An install that partially fails leaves the completed members in place — re-running
with `--force` converges (members are independently pinned; ADR 0040 records the
tradeoff).

## Keep it fresh

A bundle pins its members, so nothing moves until you say so. The update check covers
bundles alongside plugins (`GET /api/plugins/updates` → `bundles[]`): **behind** means
the bundle *repo's manifest* moved — its member pins may have moved with it.

Updating re-resolves the whole set (#2718):

```sh
python -m server plugin update-bundle my-stack            # code + lock; live after reload
python -m server plugin update-bundle my-stack --ref v2.0 # explicit target ref
```

or `POST /api/plugins/bundles/{id}/update` (what the console uses), which also
hot-reloads. Shared semantics:

- The bundle repo re-installs at its recorded ref — a **release-tag** pin moves to the
  newest semver tag (tags are immutable; re-installing the recorded one would be a
  no-op forever), a branch ref pulls its head, a SHA pin stays put. An explicitly
  passed ref is a pin request and is never replaced.
- Member pins re-resolve exactly as a fresh install; the lock row is rewritten.
- Members the new manifest **dropped** are retired when they belong only to this
  bundle; a member another bundle lists, or one you re-installed directly, is left
  alone.

**Console/API only** (the CLI is code + lock, out-of-process — a running server picks
the new code up on its next restart/reload): the declared enable set and
`config:`/`mcp:` defaults re-apply — **without undoing an operator's explicit
disable**, and never clobbering operator values — and retired members unload live.

## Uninstall one

```sh
python -m server plugin uninstall-bundle my-stack           # members + lock row
python -m server plugin uninstall-bundle my-stack --purge   # also config + secrets
```

or `DELETE /api/plugins/bundles/{id}` (hot-reloads so tools/routes actually leave the
running agent). Only the bundle's **exclusively-owned** members are removed — shared
and re-owned members stay, and the report says which. The CLI, being out-of-process,
warns when a running server keeps removed members live until its next reload.

## Publish a stack

1. **Scaffold**: `python -m server plugin new-bundle "My Stack" --member my_board=https://github.com/o/board-plugin@v0.3.0 --builtin delegates`
   (or the devkit's `scaffold_bundle` tool from chat).
2. **Pin + verify**: pins mean *"last verified working together"*
   ([ADR 0049](/adr/0049-bundle-pin-lifecycle)). Copy the reference CI at
   `examples/bundles/template/.github/workflows/verify-bundle.yml` — it smoke-installs
   the set against `verified_against`'s core and runs the scheduled **pin-bump** job
   that maintains one always-current candidate PR per stack (#2645/#2669).
3. **Ship the archetype block** so installing your bundle puts a starter card in the
   new-agent picker ([ADR 0100](/adr/0100-agent-archetypes) has the full field set) —
   see [Fleet — bundles & archetypes](/guides/fleet#bundles--archetypes--start-from-a-type).

## Related

[ADR 0040](/adr/0040-plugin-bundles) (the mechanism) ·
[ADR 0049](/adr/0049-bundle-pin-lifecycle) (pin lifecycle) ·
[ADR 0100](/adr/0100-agent-archetypes) (archetypes) ·
[Install & publish plugins](/guides/plugin-registry) (single-plugin lifecycle) ·
[Fleet](/guides/fleet) (bundles as agent starters)
