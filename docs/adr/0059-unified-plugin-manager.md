# 0059 — Unified plugin manager: in-app directory (Discover) + folded config (Installed)

Status: **Accepted** (shipped v0.48.0; extended through v0.112.0)

## Context

Plugin management is split **four ways** in the console today:

- `PluginsSurface.tsx` has three tabs — **Local** (installed list: enable/disable/
  update/uninstall), **Market** (which only **links out** to the external directory +
  a GitHub topic — nothing in-app), and **Download** (the install-from-URL form).
- A plugin's **config** (fields, secrets, Test/Connect) lives *elsewhere*, under
  **Settings ▸ Workspace ▸ Plugins** (`SettingsCategory.tsx`).

So discovering, installing, enabling, and configuring one plugin spans four screens.
Three things make now the moment to consolidate:

- **Runtime install works everywhere ([ADR 0058](./0058-runtime-plugin-install-frozen-app.md)).**
  One-click install now succeeds on every surface **including the frozen desktop
  app** — which is exactly what makes an *in-app directory* genuinely useful: browse
  and download a plugin without leaving the app, even frozen.
- **The index was always promised.** [ADR 0027](./0027-install-plugins-from-git-url.md)
  D9 + [ADR 0040](./0040-plugin-bundles.md) gestured at a curated registry on top of
  URL-install; the "Market" tab is a stub for it.
- **Config wants to live with the plugin.** The Settings ▸ Plugins panel's own empty
  hint already says *"Plugins with their own view manage settings there."*
  ([ADR 0048](./0048-settings-ia-two-scope-homes.md) put plugin config under
  Workspace.)

## Decision

**One Plugins surface, two sections — *Discover* and *Installed* — with the official
catalog served by the host and per-plugin config folded into the Installed rows.**

### D1 — Discover: an in-app official-plugin directory

Replace the link-only Market tab with a real directory rendered from the host-served
catalog (D3): cards (name · tagline · category · official badge), search + category
filter, and **one-click Install** via the existing `POST /api/plugins/install`
(runtime install, ADR 0058 — works frozen). Each card shows state: *Available /
Installed / Update available*. The trust model is unchanged ([ADR 0027 D1](./0027-install-plugins-from-git-url.md)):
install still surfaces the capability review + the "this runs code" confirm for
unofficial sources; official-catalog entries are pre-vetted.

### D2 — Installed: fold per-plugin config in

Each installed plugin is a row that expands to its **lifecycle** (enable/disable/
update/uninstall + the restart-recommended badge) **and** its **config** (fields +
Test/Connect), inline. The plugin-group rendering moves out of Settings ▸ Workspace ▸
Plugins into the Plugins surface; **Settings ▸ Plugins becomes a pointer** ("manage in
Plugins"). It reuses the data-driven schema (`settings_schema` / `pconfig`) and the
generic Test endpoint ([ADR 0029](./0029-communication-plugins-standard.md)). A plugin
that ships its **own view** ([ADR 0026](./0026-plugin-contributed-console-surfaces.md))
keeps managing settings there — its row links to the view.

### D3 — Host-served catalog (the curated index)

The host ships **`config/plugin-catalog.json`** (`id`, `name`, `repo`, `tagline`,
`category`, `official`, `latest_tag`, `install_url`) and serves **`GET
/api/plugins/catalog`**; it may refresh from a remote when online. This is
**offline-safe and frozen-desktop-safe** — browsing the directory needs no external
service. **Single official source:** the host catalog stays in sync with
`sites/marketing/data/plugins.json` (generate one from the other) so the in-app
directory and the marketing site never drift. This is the index/registry ADR 0027 D9 /
0040 always gestured at — now a *thin* layer because runtime install (ADR 0058) is the
primitive underneath.

### D4 — Structure + IA

`PluginsSurface`'s three tabs collapse to two sections (Discover / Installed);
`uiStore.PluginsTab` + the `App.tsx` tab strip update accordingly. **Install-from-URL**
moves to an **advanced action** under Installed (absorbing the Download tab).

### D5 — Bespoke affordances → data-driven (follow-up)

The remaining per-plugin frontend (e.g. the Discord Test button + guide link kept in
core) generalizes to manifest-declared fields (`guide_url`, a `connect` action) so the
consolidated surface needs **zero per-plugin frontend**.

## Consequences

- **One place** to discover → install → enable → configure a plugin. Official plugins
  (Discord, Slack, Google, Telegram, artifact, …) are installable **without leaving the
  app**, on every surface including the desktop.
- The promised **index/registry** is finally real — as a thin host-served catalog on
  top of the runtime-install primitive, not a separate marketplace service.
- **Settings IA simplifies:** Settings ▸ Plugins → a pointer; config lives with the
  plugin (matches the ADR 0048 note).
- **Subsumes** the earlier "one-click comms-channel install" (bd-3uh.3) — channels
  install from Discover like any official plugin.
- **Risk:** the catalog must track the official source + plugin releases — D3 keeps a
  single source + optional refresh to bound drift.

## Alternatives considered

- **Live-fetch the catalog from the marketing site at view time** — rejected: breaks
  offline / air-gapped / frozen-desktop. Host-served + optionally-refreshed wins.
- **Keep config in Settings, cross-link only** — rejected: the goal is one place per
  plugin; config-with-the-plugin matches the existing Settings hint.
- **Keep 3 tabs, only make Market in-app** — rejected: leaves the Download tab + the
  Settings ▸ Plugins split that the consolidation exists to remove.
- **A central server-side registry/marketplace service** — deferred: the host-served
  catalog file is the thin primitive; a hosted index is curation on top (ADR 0027's
  framing).

## Related

[0058](./0058-runtime-plugin-install-frozen-app.md) (runtime install — the enabler) ·
[0027](./0027-install-plugins-from-git-url.md) (git-install + D9 index) ·
[0040](./0040-plugin-bundles.md) (bundles) · [0049](./0049-bundle-pin-lifecycle.md)
(pin lifecycle) · [0026](./0026-plugin-contributed-console-surfaces.md) (plugin views) ·
[0019](./0019-plugin-config-settings-secrets.md) (config/settings) ·
[0029](./0029-communication-plugins-standard.md) (comms standard / generic Test) ·
[0048](./0048-settings-ia-two-scope-homes.md) (settings IA) ·
[0044](./0044-plugin-driven-console-navigation.md) (plugin-driven nav) ·
[0056](./0056-unified-dockable-view-model.md) (dockable views). Tracking: epic **bd-23a**.
