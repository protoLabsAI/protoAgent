# 0084 — Computer use: an out-of-process driver, and the fence it voids

- Status: accepted
- Date: 2026-07-16
- Deciders: Josh, agent
- Relates to: [ADR 0007](./0007-directory-aware-operator-agent.md) (filesystem fence),
  [ADR 0008](./0008-sandboxing-and-openshell.md) (sandboxing posture, egress allowlist),
  [ADR 0005](./0005-tool-pollution-and-progressive-disclosure.md) (tool pollution),
  [ADR 0019](./0019-plugin-config-settings-secrets.md) (`register_mcp_server`),
  [ADR 0027](./0027-install-plugins-from-git-url.md) (git-URL install; untrusted code → MCP),
  [ADR 0071](./0071-plugin-permissions-trust-model.md) (trust-and-consent, not sandbox)

## 1. Context & problem statement

protoAgent has no way to operate a GUI. Every tool we ship acts through a Python
function: `fetch_url` fetches, `fs_tools` reads and writes, `run_command` shells
out. Anything that only exists behind a native window — a desktop app, an
Electron thing, a web app that fights headless browsers — is simply outside what
the agent can do.

[trycua/cua](https://github.com/trycua/cua) (MIT, YC X25) is the mature
open-source option. It is three separable products, and the distinction is the
whole decision:

| Layer | What it is | Ships as |
|---|---|---|
| **`cua-driver`** | Background control of the **real** host desktop without stealing focus | standalone Rust/Swift binary, speaks **MCP** |
| **`cua-sandbox`** | Throwaway Linux/macOS/Windows/Android desktops behind one API (cloud · Docker · QEMU · Apple VZ) | Python SDK |
| **`cua-agent`** | Its own screenshot → reason → act loop | Python SDK |

This ADR is not really about which package to `pip install`. Computer use is the
first capability we would ship that **voids our containment story wholesale**,
and that deserves a decision record rather than arriving as a plugin nobody
reviewed.

### The fence this voids

ADR 0008 is explicit that protoAgent's isolation is **application-level, not
OS-enforced**. Concretely, every boundary we have lives at the Python-tool layer:

- the egress allowlist is enforced *inside* `fetch_url` (`security/egress.py`);
- the filesystem fence (ADR 0007) is an in-process path check in `tools/fs_tools.py`;
- `tools.disabled` (ADR 0005) filters *tool names*.

An agent that can move a mouse routes around all three, and not by defeating
them — by never touching them. It opens Safari and navigates anywhere; the
allowlist is not consulted, because no `fetch_url` call happens. It opens Finder
and reads anything; `resolve_project_path` is not consulted. These fences are not
weakened by computer use. They are **absent** from the code path.

That is not an argument against shipping it. ADR 0071 already locked the posture
(*"lean toward trust, no sandbox, rely on people being smart"*), and ADR 0008
already conceded app-level isolation. It is an argument for **saying so out loud**
— the failure mode ADR 0071 named as the actual risk is a *presented* model that
implies containment the *enforced* model doesn't have. A computer-use plugin
inherits that risk at maximum strength.

### The inversion worth noticing

cua's *sandbox* layer does the exact opposite. A VM is OS-enforced isolation —
strictly stronger than the OpenShell containment ADR 0008 went shopping for and
never shipped. So within one vendor:

- **`cua-driver` on the host** would be the **least**-contained tool we ship;
- **`cua-sandbox` in a VM** would be the **best**-contained tool we ship.

The cheap path is the dangerous one. That tension is the reason this is an ADR
and not a PR description.

## 2. Decision drivers

- **Don't imply a sandbox we don't have** (ADR 0071's core lesson).
- **Prove the capability is useful before paying for it.** No evidence yet that
  computer use earns its keep in our workflows.
- **Don't regress the core dependency tree**, and don't break the frozen desktop
  build (ADR 0058 — no pip at runtime).
- **Don't nest two agentic loops.** We own the tool loop in `graph/agent.py`.
- **Keep the template lean.** Host control is opinionated and dangerous; it is
  not template surface.

## 3. Considered options

**A. MCP wrapper on `cua-driver`** — a plugin whose `register_mcp_server` factory
spawns `cua-driver mcp` over stdio. Zero Python deps (external binary), works in
the frozen app, out-of-process. Controls the real desktop.

**B. Native plugin on `cua-sandbox`** — real Python tools over throwaway VMs.
Best containment; highest cost.

**C. Hand-rolled client on `cua-computer-server`'s WebSocket wire** — dodges B's
dependency cost using the `httpx`/`websockets` already in-tree, at the price of
owning a client against an unversioned 0.1.x protocol.

**D. `cua-agent` behind the delegate registry** (ADR 0025) — hand GUI tasks off
wholesale as a `delegate_to` target.

### Evidence gathered (2026-07-16)

Resolving `cua-sandbox` against our real `pyproject.toml` decides B on facts, not
taste — **our tree goes 114 → 294 packages**:

- **+154 `pyobjc-framework-*` packages** — the entire macOS umbrella, including
  HealthKit, GameKit, ShazamKit, PhotosUI. Chain:
  `cua-sandbox → cua-auto → pywinctl/pymonctl/pywinbox → pyobjc`.
- **Twisted + zope-interface**, via `vncdotool`.
- `grpcio==1.78.0` and `protobuf==6.33.6` — **exact** pins. We happen to sit on
  protobuf 6.33.6 today; that is luck, not compatibility, and it breaks on the
  next bump from either side.

`cua-sandbox` is not a thin client SDK — it bundles host automation whether or
not you want it. Consequences: it can only ever be an **optional** dep with lazy
imports (ADR 0058 forbids bundling that into the frozen app), which means the
desktop build can never have it.

Two more findings against B *right now*:

- **Cloud is sales-gated.** No public pricing; cua.ai says "request access"; auth
  is a `CUA_API_KEY` bearer. `cua-sandbox`'s best UX — zero-config cloud — is not
  actually available to us. The open path is local Docker/QEMU/Apple VZ.
- **A version trap:** the `cua` meta-package requires Python ≥3.12 and would break
  our ≥3.11 floor. `cua-sandbox` alone (0.1.17, 2026-06-24, MIT) is ≥3.11,<3.14
  and resolves clean. Never depend on the meta-package.

So B is currently *both* expensive and degraded. A is cheap and works today.

## 4. Decision

### D1 — Ship option A, out-of-process, as a **standalone** plugin

`register_mcp_server` returns an `mcp.servers[]` entry
(`{name, transport, command, args, env}`), so the integration is a manifest plus
a small factory that spawns `cua-driver mcp` over stdio. No new packages enter
the venv. ADR 0027 D1 already mandates the shape: *untrusted or dangerous code
belongs in an out-of-process MCP server*. The driver **is** one.

The plugin lives in its own repo (`cua-plugin`), not `plugins/`, matching every
other third-party integration (google, discord, terminal, claude-bridge). This is
deliberate: a fence-voiding capability should be opt-in **by installation**, not
merely by a config flag in a template everyone clones.

Corollary: **we do not install the binary.** The factory probes for `cua-driver`
and returns `None` when it is absent — the documented "server shouldn't start"
path — so the plugin is inert until an operator installs the driver *and* grants
macOS Accessibility + Screen Recording TCC. Those grants are a GUI action a human
must take; that human-in-the-loop step is a feature, not friction to design away.

### D2 — Say what it does, in the plugin's own surface

The manifest's `capabilities:` block is declarative and unenforced (ADR 0071).
For this plugin the gap between presented and enforced is at its widest, so the
description and Settings copy state plainly that enabling it lets the agent drive
the real desktop, and that the ADR 0007 filesystem fence and ADR 0008 egress
allowlist **do not apply** to anything it does. No hedging.

### D3 — Allowlist the tool surface (ADR 0005)

The driver exposes **~28** tools (`list_apps`, `list_windows`, `get_window_state`,
`launch_app`, `kill_app`, `bring_to_front`, `click`, `double_click`,
`right_click`, `drag`, `type_text`, `type_text_chars`, `press_key`, `hotkey`,
`set_value`, `scroll`, `zoom`, `page`, `get_screen_size`, `get_desktop_state`,
`get_cursor_position`, `move_cursor`, cursor/config/permission/health tools, …).
Dumping all of them into every turn is exactly the pollution ADR 0005 exists to
prevent. The plugin ships a **default allowlist** covering the documented loop
and leaves the rest opt-in via config. `tools.disabled` remains the operator's
per-row override.

### D4 — Ship the skill; the snapshot invariant is not optional

cua's own skill is blunt: *"the snapshot-before-action invariant is not optional
and silently breaks if you skip it."* The driver is **accessibility-tree-first,
not screenshot-first** — `get_window_state` returns an AX tree, you act by
`element_index`, and every action must be bracketed by a snapshot **before** (to
resolve the index, which is per-`(pid, window_id)` and stale across turns) and
**after** (to prove the action landed rather than silently no-op'd). An agent
handed these tools without that protocol will fail in ways that look like the
tools are broken.

So the plugin registers a `SKILL.md` via `register_skill_dir`. We author it
against the driver's contract rather than vendoring cua's 778-line skill and its
five companion files, which would go stale on their release cadence, not ours.

Note for anyone reading the upstream docs: **`screenshot` was removed** (cua
PR #1692). The canonical capture path is `get_window_state` with
`capture_mode: "vision"`. Summaries claiming a `screenshot` tool are stale.

### D5 — Defer B; do not build C or embed D

- **B (`cua-sandbox`)** is deferred on the §3 evidence, not rejected on merit. If
  A proves the capability earns its keep, revisit scoped to **local Docker only**
  (`trycua/cua-ubuntu` runs on our arm64 today), as an optional dep, never in the
  frozen build. It is the only path that would give us OS-enforced isolation, so
  it stays on the table.
- **C** is rejected: owning a hand-rolled client against an unversioned 0.1.x wire
  protocol is a standing maintenance tax to save a dependency we can decline by
  choosing A.
- **D (`cua-agent`)** is rejected **as a tool**: it is a competing agentic loop,
  and nesting one inside a LangGraph turn gives two schedulers one conversation.
  If we ever want it, it belongs behind the ADR 0025 delegate registry as an
  explicit `delegate_to` handoff — never as a loop inside our loop.

## 5. Consequences

**Good.**
- A real GUI capability for the cost of a manifest and a factory; no packages
  enter the venv and the frozen desktop build is unaffected.
- It lands in the shape ADR 0027 D1 already blessed — dangerous code, out of
  process, killable, swappable.
- Two untrodden seams get their first real exerciser: `register_mcp_server` has
  had **no** in-tree user (only the devkit skill mentions it), and this is the
  first plugin to ship a managed MCP server behind a binary probe.

**Bad, and accepted.**
- The most dangerous capability we ship is also the cheapest to enable. Off by
  default and standalone-by-install are the only brakes, and per ADR 0071 they
  are consent brakes, not containment.
- We inherit a third-party binary's release cadence and its self-update path
  (`check_for_update` / `cua-driver update --apply`) — the same
  human-out-of-the-loop concern ADR 0071 §1 raised about self-updating plugins,
  now one layer down where our `plugins.update_policy` cannot see it. The plugin
  does not enable driver self-update.
- macOS TCC grants attribute to the **responsible app identity**, which for the
  Tauri desktop build (ADR 0058) is the sidecar, not Terminal. Expect the grant
  UX to differ between `scripts/dev.sh` and the packaged app.

**Neutral.**
- Nothing in core changes. No new seam, no core edit — this ADR records a posture
  and a plugin lives downstream of it. If A is later abandoned, deleting the repo
  is the whole rollback.
