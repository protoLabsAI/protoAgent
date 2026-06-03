# ADR 0016 — In-app Discord configuration (token, admin list, live connect)

- **Status:** Accepted (2026-06-03)
- **Date:** 2026-06-03
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** surface, discord, config, onboarding, operator, desktop, opt-in
- **Related:** makes [ADR 0015](./0015-discord-ingress-surface.md) (the Discord surface) configurable in-app; uses the secrets-overlay + settings-schema patterns from [ADR 0013](./0013-console-data-layer-react-query.md); same opt-in posture as [ADR 0010](./0010-headless-setup-and-ui-tiers.md).

> Accepted. ADR 0015 shipped a great Discord surface — but it was **env-var only**
> (`DISCORD_BOT_TOKEN` / `DISCORD_ADMIN_IDS`), started once at boot. That's fine
> for Docker, but invisible and unreachable for someone who just installed the
> **desktop app**: there's no shell to export env into, the frozen sidecar can't
> read a repo `.env`, and a bad/absent token fails silently. Discord is now
> **configured in the app** — a token field + "Test connection" in the setup
> wizard and Settings — stored in the per-agent secrets overlay, applied live.

## 1. Context & Problem statement

ADR 0015's gateway reads `os.environ["DISCORD_BOT_TOKEN"]` directly and is
started exactly once, inside the server's startup event, opt-in by token
presence. There is no config section, no secret, no validation, no status, and
no way to change it without a process restart.

For the **bundled desktop app** this is a dead end:

- The GUI-launched sidecar has no shell env to inherit a token from, and the
  PyInstaller-frozen binary can't read the repo's gitignored `.env` (the
  `env_loader` path only helps repo/Docker runs). In practice the desktop app
  fell back to whatever `DISCORD_BOT_TOKEN` happened to be in the ambient
  environment — connecting as the **wrong bot**.
- A wrong/absent token fails **silently** (the gateway logs "disabled" and the
  user has no signal), echoing the same "silently broken" trap we just closed
  for the model API key.

A personal-assistant template whose whole pitch is "install it and talk to it on
Discord" cannot require hand-editing YAML/env to connect.

## 2. Decision

Make Discord a first-class, UI-configured surface, reusing the patterns already
in the codebase:

1. **Config + secrets.** A `discord` section in the config dataclass +
   `langgraph-config.yaml`: `enabled` (bool), `bot_token` (→ **secrets.yaml**
   overlay, added to `SECRET_PATHS`), `admin_ids` (list). The gateway reads these
   via an injected `configure(token, admin_ids)`, **falling back to the env vars**
   so Docker/headless deploys are unchanged.
2. **Live connect.** The gateway is started from the live config and **restarted
   on config reload** when the Discord fields change — so a Settings save or
   wizard finish reconnects without a process restart (same fire-and-forget-onto-
   the-loop idiom as the scheduler swap).
3. **Validation.** A real identity probe (`GET /users/@me` → bot username) behind
   `POST /api/config/test-discord`, powering a **"Test connection"** button in
   both the wizard and Settings — a bad token is caught in the UI (with the bot's
   name shown on success), mirroring the model "Test connection".
4. **Onboarding.** Discord-side setup (create app → bot → token → **Message
   Content intent** → invite) is the real friction. v1 keeps the in-app flow
   minimal — token + admin-id + Test — and links out to a **docs walkthrough**
   ([guide](/guides/discord#bot-setup)) for the full how-to. (No generated invite
   URL or guild auto-detect yet — deferred.)
5. **Scope.** **DM-first** (the assistant's working model); designating a server
   channel is a follow-up. An optional, skippable **wizard step** plus a
   persistent **Settings → Discord** section.

## 3. Consequences

- **The desktop app gets a per-user home for the token** (secrets.yaml in the
  app-config dir) — fixing the "connects as the wrong bot" problem, and making
  Discord reachable for non-developers with no env/YAML editing.
- **No silent failures**: an invalid token is caught by Test connection / on save.
- **Back-compatible**: env vars still work as a fallback; existing Docker deploys
  need no change. In-app config takes precedence when set.
- The env-only tunables from ADR 0015 (timeouts, debounce, log path) stay env-only
  for now — only token / admin_ids / enabled moved to the UI.

## 4. Alternatives considered

- **Leave it env-only, document harder.** Rejected — unreachable for the desktop
  app's target user; the frozen sidecar has no env path.
- **Full guided onboarding (generated invite URL, intent toggles, guild/channel
  picker).** Deferred — valuable, but the token + Test + docs-link covers the
  90% case; revisit once usage shows where people get stuck.
- **A dedicated "Integrations" surface.** Deferred — one Discord section in
  Settings + a wizard step is enough for a single integration; revisit when a
  second (Slack/etc.) lands.
