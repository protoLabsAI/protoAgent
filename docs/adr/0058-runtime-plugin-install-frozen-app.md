# 0058 — Runtime plugin install in the frozen desktop app (Discord as the first external comms plugin)

Status: **Accepted** (shipped v0.48.0)

## Context

[ADR 0027](./0027-install-plugins-from-git-url.md) made plugins installable from a
git URL, but `graph/plugins/installer.py` does it by shelling out to **`git`**
(fetch) and **`pip`** (`install_deps`). The **frozen desktop sidecar** (a
read-only PyInstaller bundle — [ADR 0010](./0010-headless-setup-and-ui-tiers.md)
tiers, the Tauri shell) has **neither `git` nor `pip`**, so today an external
plugin can only be installed on a dev box or server. For the desktop app, plugins
must be **baked into the bundle at build time** (`apps/desktop/sidecar/build_sidecar.py`
adds the `plugins/` tree + `--collect-all surfaces tools` so the first-party
Discord/Google surfaces load).

We want **Discord** (and other communication channels) to be an **optional
communication channel the user installs at runtime from Settings** — on *every*
surface, including the desktop app — and **never shipped in the bundle**. That
requires runtime plugin install to work inside the frozen app.

Two facts shape the design (mirroring 0027's framing):

- **The seam already reaches the frozen app.** `loader._plugin_roots()` returns
  `[bundle/plugins, live_root]` in **all** modes, frozen included; `live_root` is
  `~/.protoagent/plugins/` (or `PROTOAGENT_PLUGINS_DIR`). The loader imports each
  plugin's entry module via `importlib.util.spec_from_file_location` +
  `exec_module` — i.e. **by file path from disk**, which works in a frozen binary,
  and the loaded module imports core (`graph.*`) from the bundle. So a plugin
  dropped into `~/.protoagent/plugins/<id>/` at runtime is **already discovered,
  loaded, and run** by the frozen app — and install already auto-enables +
  **hot-reloads** routes/surfaces with no restart (`operator_api/plugin_routes.py`,
  #822). **The only frozen gaps are the two subprocess steps: `git` and `pip`.**
- **In-process plugins share the interpreter** (0027 D1). A frozen-installed plugin
  can only import what's **already in the bundle** (its own shipped code + core
  deps); it cannot pull a new native/pip dependency (no pip, read-only bundle). So
  the frozen install path is constrained to plugins whose deps are already bundled.
  **Discord qualifies**: pure-Python, and its only deps (`httpx`, `websockets`) are
  core.

## Decision

Add a **frozen-app-capable runtime install** — a **git-less HTTPS archive fetch**
plus a **bundled-dep gate** — reusing 0027's *install ≠ enable ≠ trust* model and
its pin / lock / audit machinery unchanged. Then make **Discord the first external
communication plugin**: opt-in everywhere, installed from Settings, behavior
identical to today.

### D1 — Git-less fetch via HTTPS archive (frozen / no-git)

`installer` gains an **archive fetch**: download `…/archive/<sha>.tar.gz` over
HTTPS with the bundled `httpx`, extract into the live root, and pin `<sha>` in
`plugins.lock` — an **on-disk result identical** to `git clone --depth 1` +
checkout (0027 D2/D8). Selection: prefer the archive path when `git` is
unavailable or `sys.frozen`; **`git` stays the path on dev/server** (history, ssh,
private auth). **GitHub archive URL scheme first** (matches our hosting); the fetch
is a small seam so another host or a registry can slot in later. Same review gate,
same lockfile, same pinned-SHA reproducibility as 0027.

### D2 — Bundled-dep gate replaces pip in the frozen app

0027 D4 keeps deps a separate explicit `install-deps` (pip). In the frozen app pip
is absent, so instead of installing, the installer **gates**: a plugin is
frozen-installable **iff every `requires_pip` entry is already importable** in the
runtime (checked via `importlib.util.find_spec`). A missing dep → **refuse the
frozen install** with a clear *"needs `<dep>`, not in the desktop runtime — install
on server/Docker"* message (not a cryptic enable-time ImportError). On dev/server
the 0027 pip path is unchanged. Communication channels (Discord, Slack, Telegram:
`httpx`/`websockets`, both core) all pass the gate.

> **Amendment (#1953) — an optional tier in `requires_pip`.** A manifest entry may
> be a mapping `{pkg: "pillow>=10", optional: true}` alongside the plain spec
> strings (which stay hard deps, unchanged). The D2 gate applies verbatim to hard
> deps; a missing **optional** dep **warns and installs** (warning in the install
> summary + log) instead of refusing — the tier is for a dep the plugin degrades
> gracefully without (a lazy import + a readable tool error naming the fix; the
> motivating case was protobanana refused on the desktop over a soft `pillow>=10`
> that one of its eight tools imports lazily). Non-frozen `install-deps` installs
> optional deps too, best-effort: a failed optional install warns (audited) instead
> of failing the command. The `_validate_pip_specs` rails apply to both tiers.

### D3 — Opt-in everywhere; Discord leaves the default bundle

Discord is **removed from the default bundle on all surfaces** (server bundle +
desktop sidecar). Fresh installs have no Discord; the user adds it from Settings.
The manifest's `enabled: true` "always-loaded legacy default" goes away — the
*dormant-until-a-token-is-set* behavior is replaced by *not-installed-until-chosen*,
which is **functionally identical for a user without a token**.

### D4 — Added via Settings, not onboarding

The **setup wizard stays minimal** (the onboarding-friction work) — **no channel
step**. Entry point is the existing **Settings → Plugins** panel (0027 D6:
install-from-URL → enable → hot-reload), plus a curated **one-click "official
channels"** affordance so the user picks Discord/Slack/Telegram from their pinned
repos without pasting a git URL.

### D5 — Discord extraction is a parity-preserving lift-and-shift

`protoLabsAI/discord-plugin` is the **same code relocated**, not a rewrite:
consolidate the three dirs Discord currently spans —
`plugins/discord/` + `surfaces/discord/` (gateway, conversation, context,
turn_log, return_address) + `tools/discord_tools.py` — into the external repo.
Core tie-points cut:

- **Delete** the redundant `from surfaces.discord import stop` shutdown hook
  (`server/__init__.py`) — the generic plugin-surface stop loop already runs every
  registered surface's `stop` ([ADR 0018](./0018-plugin-surfaces-routes-subagents.md)).
- **Delete** the `surfaces/` package (only Discord lived there) and drop it from
  the wheel `packages` list; move the five `tests/test_discord*.py`.
- **Keep** the bespoke Discord **frontend** affordances in core (the Test button,
  the "how to create a bot" guide link, the optional `discord?` config type). They
  **degrade gracefully** — they render only when a *Discord* settings group exists
  — so the UX is **identical** when the plugin is installed and inert when it isn't.
- **Keep** the `discord_token` redaction pattern (universal). Env fallbacks
  (`DISCORD_BOT_TOKEN`/`DISCORD_ADMIN_IDS` + the tuning vars) move with the plugin.

Result: **behavior identical once enabled.** [ADR 0015](./0015-discord-ingress-surface.md)
/ [0016](./0016-discord-ui-config.md) get an extraction note.

### D6 — Sequencing rail (don't strand desktop users)

The bundle-removal (D3) is the **last, gated** step: it must **not** land until the
git-less installer works on the desktop (parity-verified on a **signed dmg**) **and**
`protoLabsAI/discord-plugin` is **published + pinned** in `plugins.lock` — otherwise
a desktop user loses Discord with no in-app way back. Tracked as epic **bd-3uh**:
`.1` git-less installer + `.2` extraction run in parallel → `.3` Settings affordance
+ `.4` parity gate → `.5` the bundle cut.

## Consequences

- The desktop app gains **true runtime extensibility** for the class of
  pure-Python / bundled-dep plugins. Discord is the first; communication channels
  generally qualify, and the same path can later re-bundle the google/slack/telegram
  externals into the desktop runtime.
- **"Identical functionality" holds by construction** — same code, frontend stays in
  core. The only delta is *default-on → chosen-at-setup*, which is the intended
  product change.
- The frozen runtime is **not** a general plugin host: a plugin needing an unbundled
  native/pip dep still can't install there (D2 gate) — stated honestly; those route
  to server/Docker (or a future build-time bundle). Supply-chain story (pin / lock /
  audit / review) is **inherited unchanged** from 0027.

## Alternatives considered

- **Vendor pinned externals into the bundle at build time** — rejected for this goal:
  it keeps "ships with the app", not a runtime choice. Still the right path for
  dep-heavy plugins that can't pass the D2 gate.
- **Keep Discord in core** — rejected: the point is an optional external channel
  *and* proving the runtime-install path the other externals will reuse.
- **An onboarding wizard step** — rejected this iteration: keep onboarding minimal;
  Settings-only (D4).
- **Ship `git` + `pip` in the desktop bundle** — rejected: large bundle, arbitrary
  build-code at runtime (0027 D4), and still no offline native-dep compilation.

## Related

[0027](./0027-install-plugins-from-git-url.md) (git-install — extended here),
[0040](./0040-plugin-bundles.md) (bundles), [0049](./0049-bundle-pin-lifecycle.md)
(pin lifecycle), [0015](./0015-discord-ingress-surface.md) /
[0016](./0016-discord-ui-config.md) (Discord), [0018](./0018-plugin-surfaces-routes-subagents.md)
/ [0019](./0019-plugin-config-settings-secrets.md) (plugin contracts),
[0029](./0029-communication-plugins-standard.md) (communication-plugin standard),
[0010](./0010-headless-setup-and-ui-tiers.md) (UI tiers / frozen sidecar).
Tracking: epic **bd-3uh**.
