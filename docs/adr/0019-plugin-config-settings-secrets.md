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
