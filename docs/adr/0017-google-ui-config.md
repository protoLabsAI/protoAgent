# ADR 0017 — In-app Google (Gmail + Calendar) connect flow

> **Update (2026-06-17):** the Google surface has been **removed from core** and
> moved to a standalone **external plugin** (installable from its own repo). The
> in-app connect flow, OAuth-gated managed MCP server, and config/secrets/Settings
> described below are unchanged — they now live in the external plugin rather than
> `plugins/google/` + `mcp_servers/google/`. The plugin contracts this ADR relies
> on (ADR 0018/0019) made the move a no-core-edit lift-and-shift.

- **Status:** Accepted (2026-06-03); Google surface extracted to an external plugin (2026-06-17)
- **Date:** 2026-06-03
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** surface, google, oauth, mcp, onboarding, operator, desktop, opt-in
- **Related:** follows [ADR 0016](./0016-discord-ui-config.md) (in-app Discord config) as the second UI-driven integration; the surface is the Google MCP server (Slice 2); uses the secrets-overlay + settings-schema patterns from [ADR 0013](./0013-console-data-layer-react-query.md).

> Accepted. The Google surface (Gmail + Calendar) existed but its setup was
> **file + CLI** — download `credentials.json`, run `python -m
> mcp_servers.google.server` for consent, hand-edit `mcp.servers`. Unreachable
> from the desktop app (no shell, no `python` on PATH, read-only frozen bundle),
> so the agent simply had no calendar/mail. Google is now **connected in-app**:
> paste a Desktop-app OAuth client, click **Connect Google**, approve in the
> browser — token cached per-user, MCP auto-wired.

## 1. Context & Problem statement

`mcp_servers/google/` is a good MCP server, but onboarding assumed a developer
at a shell: a `credentials.json` file, a blocking `run_local_server` consent run
from the CLI, and a manual `mcp.servers` entry. In the bundled desktop app none
of that is possible — and the result is the agent telling the operator "I can't
read your calendar." The same gap [ADR 0016](./0016-discord-ui-config.md) closed
for Discord, now for Google — with the extra wrinkle of an OAuth consent dance.

## 2. Decision

1. **Config + secrets.** A `google` section (`enabled`, `client_id`,
   `client_secret` → **secrets.yaml**, `tz`). The OAuth client comes from the
   operator's Google Cloud **Desktop app** client — entered in the UI, not a
   file.
2. **Connect flow.** `POST /api/config/google/connect` runs the installed-app
   consent (`InstalledAppFlow.run_local_server`, which **opens the operator's
   browser** + loopback-captures the grant) off the event loop, caches a
   refreshable token in the **per-user config dir**, enables the surface, and
   reloads so the tools register. `GET /api/config/google/status` reports
   `{configured, connected, email}` for the UI. A **"Connect Google"** button in
   Settings (+ an OAuth-client step in the wizard) drives it.
3. **Managed MCP server.** When google is enabled **and** a token is cached, the
   config layer **auto-injects** the google MCP server entry (the operator never
   edits `mcp.servers`) and forces MCP on. The headless subprocess is **load-only**
   — it never runs consent (no surprise browser); consent is the explicit Connect
   action.
4. **Frozen desktop launch.** The bundled binary has no `python`, so the managed
   entry re-invokes the binary itself (`<binary> --mcp-google`) instead of
   `python -m mcp_servers.google.server`. The Google libs + MCP SDK are bundled
   into the sidecar.
5. **Scope.** Read Gmail + Calendar, draft-only mail (no send) — unchanged from
   Slice 2. Env/`credentials.json` remain a Docker/headless fallback.

## 3. Consequences

- The desktop app can connect Google with **no files, no CLI, no `mcp.servers`
  editing** — the operator pastes a client and clicks Connect.
- The token lives per-user (config dir), so it works in the read-only frozen
  bundle and isn't shared across forks.
- The sidecar grows by the Google client libraries (bundled). Acceptable for an
  out-of-the-box integration; still opt-in (off until connected).
- The OAuth client is still the operator's own (their Google Cloud project) — the
  template ships the *flow*, not shared credentials (quota/security).

## 4. Alternatives considered

- **Keep it file/CLI, document harder.** Rejected — unreachable for the desktop
  app's target user.
- **Ship a shared OAuth client in the template.** Rejected — shared quota +
  verification + secret-distribution problems; each operator using their own
  Desktop-app client is the Google-sanctioned path.
- **In-process Google tools instead of the MCP subprocess.** Tempting (no
  frozen-subprocess problem), but it abandons the MCP-server design Slice 2
  deliberately chose as the worked MCP example. The `--mcp-google` self-reinvoke
  keeps the architecture and works frozen.
