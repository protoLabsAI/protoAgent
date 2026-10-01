# Network egress

What protoAgent contacts **on its own** — not because you sent a message, clicked a button,
or the agent ran a tool — when it does it, where it goes, and how to turn it off. This page
backs the public claim below; if you add a background network call, add a row here.

## The claim

> **No analytics, crash-reporting, or tracking SDKs, and no usage telemetry — protoAgent
> never phones home about what you do. All agent state (chats, knowledge, tasks, config,
> secrets) lives on your disk.** Out of the box the server makes no background calls off
> your machine except to the model gateway you configured, and a fleet-discovery scan of
> your own Tailscale tailnet when Tailscale is installed. The desktop app also checks GitHub
> Releases for updates, and the console loads its fonts from Google Fonts.

Audited 2026-10 (v0.188). The dependency trees of `pyproject.toml`, `apps/web`,
`apps/desktop` and the Tauri `Cargo.toml` were searched for Sentry, PostHog, Segment,
Mixpanel, Amplitude, Plausible, Google Analytics/Tag Manager, Datadog, Bugsnag, Rollbar,
Hotjar, LogRocket, FullStory, Scarf, Aptabase and Vercel Analytics: none are present.
`opentelemetry` arrives only as a dependency of the Langfuse SDK and exports nothing unless
you configure Langfuse; `langsmith` arrives transitively via LangChain, is never imported by
protoAgent, and sends nothing unless you set `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2`.

## On by default

| What | When | Destination | Turn it off |
|---|---|---|---|
| **Desktop update check** (Tauri updater) | Desktop app launch, then every 6 h while the console is open, plus the tray's *Check for Updates…* | `https://github.com/protoLabsAI/protoAgent/releases/latest/download/latest.json` (`apps/desktop/src-tauri/tauri.conf.json`, `apps/web/src/app/UpdateNotice.tsx`) | No switch today. Desktop app only — the server, wheel and Docker image never check for updates. It is an unauthenticated GET of a public file. |
| **Console web fonts** (Geist, Geist Mono) | Every console page load, browser and desktop | `fonts.googleapis.com`, `fonts.gstatic.com` (`apps/web/index.html`) | No switch today. If they can't load (offline, blocked), the console renders in fallback fonts. No other CDN is loaded: plugin views vendor their libraries. |
| **Fleet discovery sweep** | Once at server boot | Agent-card GETs to `127.0.0.1:7860–7910` (stays on the machine), **and**, when the `tailscale` CLI is installed, the same GET to every online peer on your tailnet on ports 7860–7910 (`graph/fleet/discovery.py`) | `PROTOAGENT_DISCOVERY_DISABLE=1` disables the boot sweep; `fleet.discovery.port_min`/`port_max` narrow it. Traffic stays inside your tailnet. |
| **Model-gateway metadata** | Agent build at boot, cached per base URL | `GET {gateway}/model/info` — only the gateway **you** configured (`graph/model_window.py`) | Never leaves the endpoint you chose. |

## Only when you configure it

None of these run on a fresh install; each starts only once you set the config named.

| What | When | Destination | Enabled by |
|---|---|---|---|
| Langfuse tracing export (+ the coding-agent OTLP relay) | Every turn | Your `LANGFUSE_HOST` | A Langfuse key pair (env or `tracing:` config). `PROTOAGENT_ACP_NATIVE_TRACING=0` disables the relay alone. |
| mDNS advertise/browse (`_protoagent._tcp`) | Boot | Local network | `fleet.discovery.mdns: true` (default off; also silent when bound to loopback) |
| Delegate health probes | 15 s after boot, then every 2–16 min with backoff | Each `delegates:` entry's URL (agent card or `/models`); an ACP delegate with the default `npx -y …` command may hit `registry.npmjs.org` | Listing delegates |
| Remote fleet-member probes | While the console's fleet view polls | Each configured remote member | Adding remote members |
| Plugin update check | Console load, cached 5 min | `git ls-remote` (or the GitHub API) against each **git-installed, unpinned** plugin's own repo | Installing a plugin from a git URL (the default `plugins.lock` is empty) |
| Plugin auto-update | Every `plugins.autoupdate_interval_hours` | Those plugins' repos | `plugins.update_policy` + an interval > 0 |
| Secrets-manager sync | Boot, then every `refresh_seconds` | Infisical (`https://us.infisical.com` or your host) | `secrets_manager.enabled: true` |
| Embedding probe, prompt-cache warmer, persona-drift judge | Boot / interval | Your model gateway | `knowledge.embeddings`, cache warming, `soul.drift` |
| Lifecycle webhooks | Boot and lifecycle events | The URL you configure | Configuring a webhook |
| Telegram long-poll | Continuous | `api.telegram.org` | Enabling the Telegram plugin (off by default) |
| orgChart topology crawl | While its view polls | Your delegates/fleet peers | Enabling the orgChart plugin (off by default) |
| agent-browser CLI download | First browser tool use | `github.com/vercel-labs/agent-browser` releases | Enabling the agent-browser plugin (off by default) |

## Because you (or the agent) asked

Chat turns go to your model gateway, or to Anthropic/OpenAI directly when you sign in with
OAuth (tokens refresh against their OAuth endpoints on use). `web_search` queries DuckDuckGo;
`fetch_url`, ingestion and YouTube transcripts fetch what was asked for. Plugin installs clone
from the URL you give; *install deps* pulls from PyPI; runtime installs download Node from
`nodejs.org` or Python from `astral-sh/python-build-standalone`. A2A push notifications go to
the URL the caller supplied. The scheduler and background tasks only call this agent's own
loopback `/a2a`.

The plugin, MCP and archetype catalogs are local files shipped with the release; browsing
them fetches nothing. No bundled plugin or default config starts an MCP server.
