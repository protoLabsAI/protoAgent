# Google surface (Gmail + Calendar)

An optional **MCP server** (`mcp_servers/google/`) that gives the agent read
access to Gmail + Calendar and the ability to draft (never send) mail. Off until
you connect it. Least-privilege scopes: Gmail **readonly** + **compose**,
Calendar **readonly**.

Tools (bound as `google__…`): `gmail_search`, `gmail_read`, `gmail_draft`,
`calendar_today`, `calendar_freebusy`.

## Connect it in the app

No files, no CLI ([ADR 0017](/adr/0017-google-ui-config)):

1. **Create an OAuth client** — follow [Get an OAuth client](#oauth-client) below
   (a few minutes in the Google Cloud Console).
2. In the app, open **System → Settings → Google** (or the **Google** step in
   first-run setup) and paste the **OAuth client ID** + **client secret**.
   Optionally set your **timezone** (for correct "today" bounds). Save & apply.
3. Click **Connect Google** → your browser opens for consent → approve. The app
   caches a refreshable token in the per-user config dir, enables the surface,
   and the agent's tool list gains `google__gmail_search`,
   `google__calendar_today`, etc. — no restart.

The client secret is stored in the gitignored `secrets.yaml`; the token never
leaves the per-user config dir. Both are managed for you — Google ships as a
**first-party plugin** (`plugins/google/`, ADR 0019) that contributes a managed
MCP server via `register_mcp_server` (started only once enabled + an OAuth client
is set + a token is cached), so **you never edit `mcp.servers`**. Disable it with
`plugins: { disabled: [google] }`, or replace it with your own integration — no
core edit. See [Plugins](./plugins.md).

## <a id="oauth-client"></a>Get an OAuth client

This is the one part that happens on Google's side:

1. **[Google Cloud Console](https://console.cloud.google.com/)** → create or
   select a project → **APIs & Services**.
2. **Enable** the **Gmail API** and the **Google Calendar API** (APIs & Services
   → Library).
3. **OAuth consent screen** → **External** (or **Internal** for a Workspace) →
   add your own Google account as a **test user** (required while the app is in
   "testing").
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Copy
   the **Client ID** and **Client secret** — paste those into the app's Google
   settings. (Desktop-app clients allow the loopback redirect the consent flow
   uses, so there's nothing else to configure.)

## Notes

- **Drafts only, never send** — `gmail_draft` creates a reviewable draft; there's
  no send tool (irreversible). Send from Gmail yourself.
- **Timezone** (IANA, e.g. `America/Los_Angeles`) sets the day bounds for
  "today" — without it, bounds are UTC.
- The client secret + token are **gitignored / per-user**; never commit them.
- This surface backs the [morning briefing](/guides/scheduler) — with Google off,
  the briefing just skips the mail/calendar sections.
- **Env/CLI fallback** (Docker/headless): set the OAuth client + token path via
  env (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_TOKEN_PATH`, plus
  optional `GOOGLE_TZ`) — or `GOOGLE_CREDENTIALS_PATH` to a Desktop-app
  `credentials.json` — and mint the token once with `python -m
  mcp_servers.google.server`. The Google **plugin** then injects the managed
  server automatically; you don't add a `google` entry to `mcp.servers` (the
  plugin's entry replaces any same-named one). In a frozen desktop build the
  server is launched via `--mcp-plugin google`. The in-app flow above is the
  recommended path.
