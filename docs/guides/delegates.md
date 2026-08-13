# Delegates — the agents & endpoints your agent can talk to

A **built-in registry** ([ADR 0025](/adr/0025-unified-delegate-registry-and-panel))
that gives the lead agent **one tool — `delegate_to(target, query)`** — over a
unified roster of delegates it can hand work to:

| `type` | What it is | Dispatch |
|---|---|---|
| **a2a** | A fleet **agent** over the A2A protocol | JSON-RPC `message/send` (+ poll) |
| **openai** | An OpenAI-compatible **model endpoint** — ask another model | `POST /v1/chat/completions` |
| **acp** | A CLI **coding agent** (protoCLI, Claude Code, …) over ACP — the [supported way](/guides/coding-agents) to use one | the ADR 0024 `AcpClient` |

This unifies what used to be three separate things — `peer_consult` (a2a),
`code_with` (acp), and "no way to ask another model" — into one hot-swappable
roster.

Manage delegates three ways: the **console panel** (Workspace settings ▸
Delegates), a **REST API**, or **config** — all hot-swappable (changes apply on
the next turn, no restart). See [ADR 0025](/adr/0025-unified-delegate-registry-and-panel).

## Manage in the console (panel)

Open **Workspace settings ▸ Delegates** (a built-in — always on, alongside Tools,
MCP, and Subagents). The panel:

- **lists** your delegates with a type badge, a `secret set` / `⚠ unconfigured`
  marker, a **live health dot** (a background prober probes each delegate
  periodically — green reachable / red down / grey not-yet-checked), and a per-row
  **Test** button for an on-demand probe. For an `acp` (coding-agent) delegate the
  probe runs only the ACP `initialize` handshake — **not** a session — so it's cheap
  and side-effect-free, never opening a thread against the agent on a timer
  ([#1300](https://github.com/protoLabsAI/protoAgent/issues/1300));
- shows a **`last call failed`** pill when the most recent real dispatch raised, with
  the reason on hover. This is a *different* question from the health dot, and the two
  can disagree: because an `acp` probe stops at the handshake, a coder whose binary
  launches fine but fails every *session* shows **green** while every `delegate_to`
  call fails. The pill is where that shows up outside the chat that triggered it. It
  clears on the next successful dispatch, and only a failed dispatch counts — stopping
  a turn isn't the delegate's fault, and a coder that runs but reports it couldn't do
  the job dispatched fine;
- **adds** one via a **type picker** (A2A agent / Model endpoint / Coding agent)
  and a form generated from each type's field schema;
- **edits / deletes** existing ones; secrets you enter are routed to
  `secrets.yaml` and never shown back (the form says *"set — leave blank to keep"*).

Saving writes the config + secret and hot-reloads, so the new roster is live on
the next turn.

## Declare delegates

The registry is a **built-in** — always on, can't be disabled, and managed in
**Workspace settings ▸ Delegates** (no plugin to install or turn on). It does
nothing until you declare a delegate, so just add entries:

```yaml
# config/langgraph-config.yaml
delegates:
  - name: helm                      # the name the LLM passes to delegate_to(target=…)
    type: a2a
    description: Chief of staff — planning, fleet coordination.
    url: https://helm.example/a2a
    auth: { scheme: bearer }        # token from secrets.yaml (below) or *_env

  - name: opus
    type: openai
    description: Heavy reasoning model for deep analysis.
    url: https://api.proto-labs.ai/v1
    model: protolabs/reasoning
    system_prompt: "Answer thoroughly but concisely."

  - name: proto
    type: acp
    description: Terminal coding agent for this repo.
    command: proto
    args: ["--acp"]
    workdir: ~/dev/my-repo
    permissions: allowlist          # auto | allowlist | readonly (see ADR 0024)
```

`delegates` is a **top-level list** (ORBIS-style), not a plugin config section.
Editing it and hitting **Save & Reload** rebuilds the roster live — no restart
(protoAgent re-runs the plugin's `register()` with the new config).

## Use it

```
delegate_to(target="opus", query="What are the trade-offs of X vs Y? Be concise.")
delegate_to(target="proto", query="Add a /healthz route and run the tests.")
delegate_to(target="helm", query="What's the current sprint status?")
```

The configured delegate names + descriptions appear in the tool's description, so
the model knows what it can reach. Each delegate is stateless from the caller's
view — the `query` must be self-contained (the delegate doesn't see this chat).

### Foreground vs background

`delegate_to(..., background=True)` runs the delegation detached: the tool returns
a job handle immediately and the delegate's reply is delivered back on a later
turn, so a slow delegate (a coding agent building a PR) never holds the caller's
turn open. Prefer it for anything that may take more than a couple of seconds, and
for any fan-out across several delegates.

**Either way the reply arrives whole.** A delegate's reply is the deliverable you
dispatched, so the background path delivers it in full rather than excerpting it
the way an unsolicited subagent *report* is excerpted (ADR 0070 D2, amended by
#2363). Background vs foreground changes when the answer arrives, never how much
of it you get.

### When a coder's reply is cut short

protoAgent never truncates a delegate's reply — but the delegate itself can stop
early. A coding agent may hit its output-token limit mid-generation, or decline the
request outright. Those replies come back with an explicit `[incomplete reply — …]`
note appended, so the delegating agent can tell a truncated answer from a finished
one and re-dispatch the remainder rather than acting on half a result. A normal
completion carries no marker.

## Secrets

Auth tokens / API keys are stored in the gitignored `config/secrets.yaml`, never
in the tracked config or in API responses — the same handling as the Discord /
Google tokens. For PR1 you can either:

- set the value in `secrets.yaml` (merged into the delegate at load), or
- reference an env var: `auth: { scheme: bearer, credentialsEnv: HELM_TOKEN }`
  (a2a) / `api_key_env: GATEWAY_KEY` (openai).

## TLS trust

Delegate calls over HTTPS verify through your OS's own certificate store (Windows
cert store, macOS Keychain, a Linux distro bundle) as well as the public root list
— not certifi alone. This makes a peer behind an internal CA, an enterprise
TLS-terminating proxy, or a home-lab reverse proxy with a locally-trusted cert work
the same way it already does in your browser: install the CA where the OS trusts
it, and a delegate probe/dispatch to that peer trusts it too. A chain the OS itself
doesn't trust still fails closed — there is no setting that disables verification
(#2643).

If you were previously pointing an `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
`CURL_CA_BUNDLE` env var at a custom CA bundle to get a private CA trusted: on
Windows and macOS that override no longer reaches delegate calls, since the OS
trust APIs verify independently of it. Install the CA in the OS store instead —
it keeps working on Linux only because the OS trust path there happens to read
the same variable, not because it's a supported override mechanism.

## Manage via the REST API

The plugin mounts a CRUD surface (operator-console posture — localhost-default,
bearer-when-exposed, like `/api/config`). The console panel (PR3) is built on it;
you can also drive it directly:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/delegate-types` | type list + field schema (drives the form) |
| GET | `/api/delegates` | list delegates (secret-free; `configured` + `has_secret` flags) |
| POST | `/api/delegates` | create (409 if the name exists) |
| PUT | `/api/delegates/{name}` | update |
| DELETE | `/api/delegates/{name}` | remove |
| POST | `/api/delegates/test` | reachability probe of an entry (the **Test** button) |

Create/update/delete **write the config + route the secret to `secrets.yaml`**,
then hot-reload — so the roster is live on the next turn, no restart. A secret you
send in `auth.token` / `api_key` is stored under the `delegate_secrets` overlay
and **never returned** by `GET /api/delegates`; `has_secret` tells the panel one
is stored.

```bash
curl -s localhost:7870/api/delegate-types | jq '.types[].type'
curl -s -X POST localhost:7870/api/delegates -d '{"name":"opus","type":"openai",
  "url":"https://api.proto-labs.ai/v1","model":"protolabs/reasoning","api_key":"…"}'
curl -s -X POST localhost:7870/api/delegates/test -d '{"type":"a2a","url":"https://peer/a2a"}'
```

## Relationship to `code_with` / `peer_consult`

`delegate_to` supersedes them: an `acp` delegate is what `code_with` did, and an
`a2a` delegate is what `peer_consult` did. **`code_with` has been removed** (the
`coding_agent` plugin is now just the shared ACP client library); `peer_consult`
remains, deprecated, for back-compat. New setups use `delegates` + `delegate_to`.
