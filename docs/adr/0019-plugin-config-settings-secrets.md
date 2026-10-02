# ADR 0019 — Plugins contribute config, settings & secrets

- **Status:** Accepted (2026-06-04)
- **Date:** 2026-06-04
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** extensibility, plugins, config, settings, secrets, fork, architecture
- **Related:** completes the plugin reach started in [ADR 0001](./0001-extensibility-and-plugin-architecture.md) (tools+skills) and [ADR 0018](./0018-plugin-surfaces-routes-subagents.md) (surfaces/routes/subagents); prerequisite for migrating the [Discord](./0015-discord-ingress-surface.md)/[Google](./0017-google-ui-config.md) surfaces to plugins (#509).

> Accepted. ADR 0018 let a plugin contribute a surface/route/subagent, but a
> *configurable* surface (a Discord-style gateway) still needs **core edits** for
> its config — the `discord_*`/`google_*` dataclass fields, `SECRET_PATHS`, the
> Settings-schema group. So a fork's own ingress is half a plugin. Close the gap:
> a plugin **declares its config in the manifest** and gets a typed config
> section + secrets routing + a Settings group, with no `config.py` /
> `config_io.py` / `settings_schema.py` edit.

## 1. Context & Problem statement

`LangGraphConfig.from_yaml` parses a fixed set of sections into a flat dataclass;
`SECRET_PATHS` (a constant) routes named keys to the untracked `secrets.yaml`;
`settings_schema.FIELDS` (a constant) drives System → Settings. All three are
**closed** — a plugin can't add to them, so a configurable plugin must edit core.

The ordering constraint is the crux: config/secrets/settings must be known
**before** a plugin's `register()` code runs (config is loaded at boot; secrets
are stripped on every save; the settings schema is built on demand). So plugin
config can't be declared imperatively in `register()` — it must be **data**,
available at manifest-discovery time without importing the plugin.

## 2. Decision

A plugin **declares its config in `protoagent.plugin.yaml`** (parsed at discovery,
no import):

```yaml
config_section: discord          # top-level YAML section (default: the plugin id)
config:                          # defaults for that section
  enabled: false
  admin_ids: []
secrets: [bot_token]             # keys in the section routed to secrets.yaml
settings:                        # Settings-schema fields (System → Settings group)
  - { key: enabled,    label: "Enable",    type: bool }
  - { key: bot_token,  label: "Bot token", type: secret }
  - { key: admin_ids,  label: "Admin IDs", type: string_list }
```

A plugin **claims a top-level section** (not a nested `plugins.<id>` bag) — this
matches how Discord/Google already store config (`discord:` / `google:`), so the
migration (#509) is a lift, not a config move. The loader rejects a section that
collides with a built-in (logged + skipped).

Wiring:

- **Config.** `LangGraphConfig` gains `plugin_config: dict[section → dict]`.
  `from_yaml` reads each discovered plugin section, overlays the YAML on the
  manifest defaults, and resolves secrets from the overlay. The plugin reads its
  own config via `config.plugin_config["<section>"]` (passed to its surface/route).
- **Secrets.** `config_io.secret_paths()` returns the base `SECRET_PATHS` **plus**
  each plugin's `(section, secret_key)` pairs; `split_secret_updates` /
  `strip_secrets_from_doc` use it, so a plugin secret is stripped to `secrets.yaml`
  exactly like the model API key. `config_to_dict` includes plugin sections
  (secrets redacted).
- **Settings.** `build_schema` appends each plugin's declared fields under a group
  named for the plugin (keys namespaced `<section>.<key>`), so they render +
  save through the existing generic Settings surface — no per-plugin UI code.

The **wizard step is out of scope** (deferred) — Settings + a docs link is enough;
a schema-driven generic wizard step can come later.

## 3. Consequences

- A fork ships a **fully self-contained configurable surface** as a plugin —
  config + secrets + Settings + (ADR 0018) surface/routes/tools — with **zero**
  core edits. Closes the last extensibility gap.
- The **config + config_io + settings_schema layers gain a dependency on plugin
  manifest discovery** (data-only — no plugin import), so the closed constants
  become "base + plugin-declared". Discovery is cached; manifests are pure YAML.
- A plugin section that collides with a built-in is rejected — built-ins win.
- `plugins.enabled` changes still need a restart (config sections are resolved at
  boot) — consistent with ADR 0018.

## 3a. Addendum — managed MCP servers + host additions (#509)

Migrating the **Google** surface to a plugin needed one capability ADR 0018's
contribution set didn't cover: the agent reaches Google through a *managed MCP
server*, injected into MCP discovery based on live config (OAuth-gated, started
only once a token exists). Added under this ADR:

- **`register_mcp_server(factory)`** — a plugin contributes a factory
  `factory(config) -> entry | None`, called at every graph build with the live
  `LangGraphConfig`. A returned entry is injected like a configured `mcp.servers`
  entry (and replaces a same-named one); its presence activates MCP even when
  `mcp.enabled` is off. Plugins now load **before** MCP discovery so their
  factories are available (MCP tools are namespaced `<server>__<tool>`, so the
  earlier collision set is unaffected).
- **Generic frozen entrypoint** — the google-specific `--mcp-google` shim became
  `--mcp-plugin <id>`, which imports the named plugin's module and calls its
  `mcp_main()`. No core reference to any specific plugin remains.
- **Host additions** — `host.config()` (the live config) + `host.apply_settings(
  patch)` (persist + reload) so a plugin *route* can read live config and apply a
  change (Google's Connect flow flips `enabled` and reloads). The desktop sidecar
  bundles the `plugins/` tree so plugins load in the frozen app.

## 3b. Addendum — settings that name a program to spawn (2026-10)

The agent's guarded `set_config` tool (`tools.self_config_enabled`) may write plugin
sections — that is how an agent repoints a board at a different coder. It must never
*define* an executable, and a plugin section can't be enumerated in advance, so the
fence works on key names below the section, plus one manifest field:

- **Name rule (convention, no author action).** A key segment that *is*, or has as a
  `_`/`-`/camelCase token, one of `command` `cmd` `args` `argv` `binary` `bin` `exe`
  `executable` `interpreter` `entrypoint` is refused — `local_gate_cmd`, `proxy_command`,
  `rh_bin`, `binary_path`, `browserArgs`. Applies at any depth, including keys hidden
  inside a dict or list *value*.
- **`settings[].spawns: true` (author declaration).** For a key the name rule can't see
  (`ffmpeg_path`), the plugin marks its setting `spawns: true`. The fence reads the marker
  from **every installed** plugin, enabled or not (a value planted while a plugin is off
  would otherwise spawn when it's turned on), and refuses plugin-section writes if that
  discovery fails. Older cores ignore the field, so it is safe to declare unconditionally.
- **Not a blanket `*_path` rule.** Across the bundled plugins and the catalog, `*_path`
  keys are mostly *data* (`brand_kit_path` in two plugins); refusing them would take away
  repointing that is legitimately the agent's. Every spawn-naming setting surveyed is
  caught by the name rule except `ffmpeg_path`, which declares the marker.

This fences only the *agent's* writer. The operator sets these keys freely from
Settings, the CLI, and `PATCH /api/config`.

## 4. Alternatives considered

- **Imperative `register_config()` in `register()`.** Rejected — config/secrets/
  settings are needed *before* `register()` runs (boot/save/schema time); a
  plugin's module may even need its config to import. Manifest data is available
  at discovery, ahead of any import.
- **Namespaced `plugins.config.<id>` bag** instead of a claimed top-level section.
  Cleaner isolation, but changes where Discord/Google store config (breaks the
  migration + existing configs) and needs nested secret-path handling. The
  claimed top-level section matches the status quo and keeps secret paths flat.
- **A typed dataclass field per plugin (generated).** Rejected — can't generate
  dataclass fields for unknown plugins at import time; the `plugin_config` dict is
  the honest shape for open extension.
