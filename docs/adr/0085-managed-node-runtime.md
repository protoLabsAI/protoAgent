# 0085 — Managed Node.js runtime, provisioned on demand

- Status: accepted
- Date: 2026-07-17
- Deciders: Josh, agent

## 1. Context & problem statement

A whole class of protoAgent extensions launches through Node's `npx`: the ACP
coding agents driven via the fetch form (`npx -y @agentclientprotocol/claude-agent-acp`
for Claude Code, `npx -y @zed-industries/codex-acp` for Codex — `runtime/acp_agents.py`),
and most of the community MCP servers in `config/mcp-catalog.json`. All of them
need `node`/`npx` on the PATH of the process that spawns them.

The desktop app doesn't provide one. It bundles only the Python server sidecar
(`tauri.conf.json` `externalBin`), and the existing PATH machinery —
`augmented_sidecar_path()` in the Tauri shell (#1299) and
`acp_client._discovered_node_dirs()` on the Python side — can only *find* a Node
the user already installed (Homebrew, nvm, fnm, Volta, asdf). On a clean machine
with no developer toolchain there is nothing to find, so those features dead-end
with *"agent binary not found: 'npx'"*. A desktop user reasonably expects "npx
functionality" to work on download without hand-installing Node into their shell.

The default experience does **not** need Node — the native runtime is the
LangGraph loop talking to the LiteLLM gateway, entirely inside the Python
sidecar. Node is an *opt-in advanced* dependency (ACP coding agents + npx MCP
servers). So the bar isn't "the app is broken", it's "an opt-in capability
silently requires a toolchain the user doesn't have, and fails opaquely".

## 2. Decision drivers

- Meet the "works on download" expectation for the npx-based features without
  making every user pay for a runtime they may never use.
- Keep the installer lean and side-step signing/notarizing a second bundled
  Mach-O binary inside the hardened-runtime app.
- One consumption seam, not a patch at every subprocess launch site — ACP
  (`acp_client._launch_env`) and MCP (`mcp_tools._inherited_env`) build their
  child env independently today, and future spawn sites shouldn't have to know.
- A real integrity gate on a downloaded, executed runtime — not
  trust-on-first-use.
- Respect the import-layering invariant: `infra`/`runtime` must not reach into
  `server`/`operator_api`.

## 3. Considered options

1. **Bundle Node in the desktop app** — ship a Node runtime as a second Tauri
   sidecar. Most literal answer, but grows every installer by the runtime,
   adds a binary to sign/notarize per platform, and pays the cost for users who
   never touch the feature.
2. **Bundle Node + pre-install the adapters** — fully offline, but 100 MB+ (the
   Claude adapter carries the whole Code SDK) and makes us own those adapters'
   version + security cadence, adjacent to the "don't vendor Anthropic assets"
   line we already hold.
3. **Provision on demand (chosen)** — download a pinned Node into the
   box-shared data dir when the user asks for it (one CLI command / one console
   click), and teach the existing PATH seam about it. Lean installer, no bundled
   binary to notarize, we control the exact version. Costs a one-time download
   when the user opts in.

## 4. Decision

Adopt option 3.

**Where it lives.** The runtime is extracted to `box_root/runtime/node/current`
(box tier, not instance tier — one machine provisions Node once and the default
instance, the dev sandbox, and every fleet member share it; a per-instance copy
would waste ~150 MB each and re-download on every `dev-reset`). `current/` is a
real directory (not a symlink — Windows symlink creation needs elevation), so
consumers resolve `current/bin` (POSIX) / `current` (Windows) without knowing the
pinned version.

**Two halves, split by layer.**

- `infra/node_runtime.py` — the light, dependency-free half every layer may
  import: `managed_node_bin_dir()` (the bin dir of a *working* install, else
  None) and `augment_path_with_managed_node()`, the single consumption seam.
- `runtime/node_install.py` — the installer: download a pinned release from
  nodejs.org, verify it against an **in-repo SHA256 table**, extract, and swap it
  into `current/` (old install kept until the new one is in place). Consumed by
  the CLI and, later, an operator endpoint.

**The consumption seam is a boot-time PATH append.** At server start
(`server.agent_init`) we call `augment_path_with_managed_node()`: if `node`
isn't already resolvable and a managed install exists, we **append** its bin dir
to the process `os.environ["PATH"]`. Append (not prepend) so a user's own Node
always wins; process-level (not per-launch) so ACP, MCP, delegates, and `gh`/git
inherit it uniformly. The installer re-runs the same augmentation on success, so
a live server hot-adopts a freshly provisioned runtime without a restart.

**Integrity.** We pin `NODE_VERSION` and a SHA256 per supported
`(platform, arch)` in `_SHA256`, checked in from
`https://nodejs.org/dist/<version>/SHASUMS256.txt`. Verifying against a digest we
already committed — rather than the `SHASUMS256.txt` the same host serves — is
what makes this a real gate: a compromised mirror can't swap the binary without
also matching a hash in the repo. Archive members are additionally guarded
against absolute/`..` paths, and tar extraction uses `filter='data'` where
available.

**Surfaces.** `protoagent runtime install-node` provisions it; `runtime list`
shows Node status (`system` / `managed` / not-found) and reflects the managed
runtime in the per-agent install probes. Supported targets: darwin arm64/x64,
linux arm64/x64, win x64. A one-click console button + operator endpoint is a
follow-up.

## 5. Consequences

- **Positive.** The npx-based coding agents and MCP servers work on a clean
  desktop machine after one opt-in step; installer stays lean; no second binary
  to notarize; the version is ours to pin and bump; one seam covers every
  current and future subprocess launch.
- **Negative / trade-offs.** The first use still needs network (the Node
  download, and `npx -y` still fetches the adapter on first launch, then caches
  in `~/.npm/_npx`); bumping Node means updating `NODE_VERSION` **and** the
  `_SHA256` table together; the on-demand step is a real (if one-time) wait, not
  invisible.
- **Not chosen.** We do not vendor the ACP adapters — `npx -y` fetch-on-demand
  keeps us out of owning the Claude/Codex SDK release cadence.

## 6. References

- ADR 0024 / 0025 — CLI coding agents over ACP; the unified delegate registry.
- ADR 0033 — ACP-as-runtime.
- ADR 0004 — instance/box path scoping (`infra/paths.py`).
- #1299 — the Tauri sidecar login-shell PATH fix (finds an existing Node; this
  ADR provides one when none exists).
- `docs/guides/coding-agents.md`, `docs/guides/mcp.md`.
