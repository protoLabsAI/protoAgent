# Fleet console — front-end handoff (ADR 0042)

For whoever builds the native-app fleet panels. The **backend control plane is live**
(protoAgent PR #787) — you can build the onboarding / switcher / fleet-manager panels
against it **today**. This doc is the contract + the task split + what's still backend-pending.

Read alongside: [ADR 0042](../adr/0042-fleet-supervisor-unified-console.md) (§B API, §E switcher,
§H native desktop) and the [Fleet guide](../guides/fleet.md).

---

## 1. The API you code against (live now — #787)

All JSON. Errors come back as **HTTP 400** with `{"detail": "<readable message>"}` — show it
inline, never expect a 500 for user error.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /api/fleet` | — | `{agents: [Agent]}` |
| `POST /api/fleet` | `{name, bundle?, port?, start?=true, shared_skills?}` | `{ok, agent: Agent, installed: [pluginId]}` |
| `POST /api/fleet/{name}/start` | — | `{ok, agent: Agent}` |
| `POST /api/fleet/{name}/stop` | — | `{ok, name, stopped: true}` |
| `DELETE /api/fleet/{name}` | `?purge=true` (also wipe its data) | `{ok, name, removed: [...]}` |
| `GET /api/archetypes` | — | `{archetypes: [Archetype]}` |

```ts
type Agent = {
  name: string;          // also the instance id; unique, [A-Za-z0-9-_]
  id: string;
  port: number;
  pid: number | null;    // null when stopped
  running: boolean;
  bundle: string;        // "" for a Basic agent
};

type Archetype = {
  id: string;            // "basic", or a bundle id e.g. "pm-stack"
  label: string;         // "Basic", "Project Manager"
  icon: string;          // lucide-react icon name (e.g. "Sparkles", "LayoutGrid")
  blurb: string;
  bundle: string | null; // null = Basic (no bundle); else the bundle git URL to pass to create
};
```

**Create flow:** the archetype picker lists `GET /api/archetypes`; on submit, `POST /api/fleet`
with `{name, bundle: archetype.bundle}` (omit/`null` bundle = Basic). `start: true` (default)
creates **and** launches it. Creating from a bundle clones+installs it (a few seconds) — show a
spinner; the request returns when the agent is up.

---

## 2. Panels to build (parallelizable now)

All three are buildable against the API above with **no backend dependency** beyond #787.

1. **Onboarding / "New agent"** — archetype cards from `GET /api/archetypes` (render `icon` via
   lucide-react, `label`, `blurb`) → name field → `POST /api/fleet`. First-run shows this instead
   of the single-agent setup wizard; it's also the "+ New agent" target from the switcher.
2. **Fleet manager** (Settings → **Agents**) — `GET /api/fleet` rows with a status dot
   (`running`), port, pid; per-row **Start** / **Stop** (`/start`,`/stop`) and **Remove**
   (`DELETE`, with a purge confirm); a **+ New** that opens panel 1. Poll `GET /api/fleet` (~3s) or
   refetch after each action.
3. **Topbar switcher** — the agent-name dropdown: `GET /api/fleet` list + status dots + "+ New
   agent". You can build the **list + selection UI** now; the actual *view-switch* lands with the
   proxy (§3).

Per-agent **config** is **not new** — it's the existing Settings drawer (model/plugins/secrets),
just pointed at the active agent's workspace. No new panel needed.

---

## 3. In-place switch — **live now** (#788)

The switcher's actual *view-switch* is ready:

```
POST /api/fleet/{name}/activate   point the console proxy at a running agent
GET  /api/fleet/active            → {active: <name>|null}   (also on GET /api/fleet)
ANY  /active/<path>               reverse-proxy the console to the active agent
```

- Talk to the **active** agent via the **`/active/<path>`** prefix: chat → `/active/api/chat`,
  SSE → `/active/api/events`. Switching = one `activate` call; the caller's URL never changes.
- `409` if nothing is active, `502` if the active agent died — surface either inline.
- **Two planes:** `/active/*` is the human's lens only. Each agent stays an **independent A2A
  endpoint on its own port** (`GET /api/fleet` exposes each agent's `a2a` URL), reachable
  regardless of focus — so agent↔agent `delegate_to` goes direct, never through the proxy.

## 3a. Per-agent theme — **live now** (#789)

Each agent saves its own look; the switch repaints automatically because theme reads/writes go
through the proxy to the focused agent.

```
GET    /active/api/theme    → {theme: <blob>|null}   the focused agent's saved theme
PUT    /active/api/theme    {theme:{...}} | <blob>   persist it
DELETE /active/api/theme                              reset to defaults
```

- **Pull the ThemePanel over** (@protolabsai/ui 0.17.0).
- **On focus / after `activate`** → `GET /active/api/theme`; apply the blob if non-null, else
  defaults — so each agent shows its own theme.
- **On ThemePanel save** → `PUT /active/api/theme` with the token blob.
- Storage is **opaque** — the panel owns the token schema; the server just round-trips the JSON,
  so new tokens/formats (oklch, rgba, …) need no server change.

## 3b. Still backend-pending

- **Tauri shell as hub** (desktop/Rust) — spawning agent sidecars + proxy plumbing in the shell is
  the desktop team's piece (it reuses the existing server-sidecar machinery). The shell can also
  just sidecar a thin Python hub; either way it talks to the same API above.
- **Keep-alive policy** (keep-N-warm) — mine, in progress (slice 5). Transparent to the panels.

---

## 4. Notes / gotchas

- **Names** are the identity + the URL path param — validate `[A-Za-z0-9-_]`, non-empty (the
  server rejects bad names with a 400 you can surface).
- **Ports** auto-assign if you omit `port`; the panel rarely needs to set one.
- **Status truth** is `running` (a live-pid probe) — a crashed agent flips to `running:false` on
  the next `GET /api/fleet`, so polling is the source of truth, not the last action's response.
- **Icons** are passed as lucide-react names; fall back to a default (e.g. `Package`) if unknown.
- **Bundle catalog** today = installed bundles + Basic. A "browse more archetypes" catalog (offer
  uninstalled bundles) is a later enhancement — for now the picker shows Basic + what's installed.

---

## 5. TL;DR task split

| Who | What |
|---|---|
| **Front-end (you)** | Onboarding/archetype picker · fleet manager · switcher (list + **switch via `activate`**) · **ThemePanel** wired to `/active/api/theme` — all over live endpoints |
| **Me (backend)** | ✅ control-plane API (#787) · ✅ reverse proxy + `activate` (#788) · ✅ per-agent theme (#789) · keep-alive policy (slice 5, in progress) |
| **Desktop/Rust** | Tauri shell as hub: agent sidecars + proxy plumbing (ADR 0042 §H) |
