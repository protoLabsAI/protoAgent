# ADR 0071 — Plugin permissions: a trust-and-consent model, not a runtime sandbox

- **Status:** Proposed (2026-07-03) — design-only; the consent layer (§3 D3) is a tracked follow-up, no runtime code lands with this ADR.
- **Deciders:** core maintainers
- **Issue:** #1725
- **Relates to:** [ADR 0001](./0001-extensibility-and-plugin-architecture.md) (plugin architecture), [ADR 0018](./0018-plugin-surfaces-routes-subagents.md) (backend seams), [ADR 0019](./0019-plugin-config-settings-secrets.md) (config/secrets), [ADR 0027](./0027-install-plugins-from-git-url.md) (git-URL install, `install ≠ enable ≠ trust`), [ADR 0008](./0008-sandboxing-and-openshell.md) (sandboxing posture), [ADR 0005](./0005-tool-pollution-and-progressive-disclosure.md) (`tools.disabled`)

## 1. Context & Problem statement

protoAgent plugins are the whole extensibility premise (ADR 0001): a plugin is a
directory with a manifest and a `register(registry)` entry point that the loader
**imports in-process** and calls with the runtime's full privileges
(`graph/plugins/loader.py`). Through `PluginRegistry` a plugin can register tools
the model calls autonomously, FastAPI routers (arbitrary in-process HTTP
handlers), long-lived background surfaces, middleware that wraps **every** turn,
subagents, MCP servers, goal/watch verifiers, the knowledge/embedder backend, and
host services including `apply_settings` (rewrite config + reload the graph). See
the as-built inventory in §2.

Two things make "what is a plugin allowed to do, and what does the operator
consent to?" newly pressing:

1. **Self-updating plugins.** Opt-in background auto-update shipped this cycle
   (#1720): a plugin the operator lists in `plugins.update_policy` now pulls new
   code from its source and hot-reloads **without a human in the loop each time**.
   That is only sound if "I trust this source" is a decision the operator actually
   made and the system recorded.
2. **The manifest advertises capabilities it does not enforce.** `capabilities:`
   in the manifest is surfaced in the console install review, but the code is
   explicit that it is *"declarative, for transparency… not yet enforced"*
   (`graph/plugins/manifest.py`). Reading the console, an operator could
   reasonably believe a plugin declaring `network: [api.foo.com]` is *confined*
   to that host. It is not. That gap between the presented model and the enforced
   model is the actual risk — not the in-process execution itself.

The maintainer posture on containment was **locked on 2026-06-10**: *ComfyUI
model — lean toward trust, no sandbox, rely on people being smart, don't fight
it.* ADR 0027 D1 already encoded the corollary (git-URL plugins are trusted,
reviewed code; **untrusted** code belongs in an out-of-process **MCP** server,
which is sandboxable). This ADR does not relitigate that. It **consolidates the
posture into an explicit permissions model**, makes the presented model match the
enforced model (stop implying a sandbox we don't have), and **formalizes the
provenance-based consent layer** that was designed as "P1 trust-acks" but never
built.

## 2. Evidence — the as-built enforcement surface

An enabled plugin runs in-process with full runtime privileges. Against that
baseline, the **only hard boundaries** that exist today are:

| # | Boundary | Where | Scope |
|---|---|---|---|
| a | **Enable gate + `plugins.disabled`** — a third-party plugin loads only via `plugins.enabled` or its own `enabled: true`; `disabled` wins over both | `graph/plugins/loader.py:315` | Whole-plugin on/off |
| b | **`tools.disabled` denylist** (ADR 0005) applied over the fully assembled set | `tools/lg_tools.py`, `graph/agent.py` | A plugin **tool**, by name only |
| c | **Route auth default-deny** + namespace-scoped `public_paths` (a plugin may exempt only paths under its own `/plugins/<id>/` · `/api/plugins/<id>/`) | `a2a_impl/auth.py`, `graph/plugins/manifest.py` | HTTP surface |
| d | **Load gates** — `requires_env` (missing ⇒ skip) and `min_protoagent_version` (refused before import) | `graph/plugins/loader.py` | Load-time only |
| e | **Install gates** — optional, **default-open** source allowlist (`plugins.sources.allow` globs), SHA pinning in `plugins.lock`, tar-traversal / pip-spec / ref / URL validation | `graph/plugins/installer.py` | Install-time only |

Everything else is **unenforceable in-process** and governed only by the
declared-surface *convention* plus the trust-by-default posture:

- **Filesystem:** the `filesystem.*` fence (ADR 0007) governs only the built-in
  fs tools. Plugin code using plain `open()` reads/writes anywhere.
- **Network egress:** the allowlist lives only inside the built-in `fetch_url`
  tool (`security/egress.py`). Plugin code using `httpx`/`socket` bypasses it.
- **Secrets:** the config API hands a plugin only its own resolved section
  (ADR 0019), but in-process it can `from runtime.state import STATE` and read
  every plugin's section + call `registry.host.config()` for the whole live
  config. Section scoping is an API convenience, not a boundary.
- **Internal APIs / config:** a plugin router runs with full server privilege and
  can call any internal module, including `apply_settings` to rewrite YAML and
  flip any setting (`graph/plugins/host.py`).
- **Modules:** no import allowlist; a plugin can import anything and spawn
  subprocesses.

Two posture facts complete the picture: (1) the console install path
**auto-enables** (#899), collapsing `install → enable → trust` into one click
when no `sources.allow` is set; and (2) **"P1 trust-acks" was designed but never
built** — `to_yaml` persists `plugins.enabled/disabled/dir/sources.allow`, but
there is **no** `sources.official`, `sources.acked`, or `trust_unverified` field,
and no consent dialog or ack API anywhere.

## 3. Decision

### D1 — Trust, not sandbox (reaffirmed and made explicit)

In-process plugins are **trusted code**, like a pip dependency you reviewed. We
will **not** build a runtime capability sandbox for in-process plugins. A sandbox
that a plugin's own Python cannot trivially escape is not achievable in-process
without a fundamentally different execution model (subprocess / OS isolation /
WASM), and pursuing one contradicts the locked ComfyUI posture and the plugin
authoring DX ("it's just Python"). **The sanctioned path for untrusted code
remains MCP** (out-of-process, declared tools, already sandboxable — ADR 0027 D1,
ADR 0008). This ADR closes the door on periodically re-proposing an in-process
sandbox: the answer is MCP.

### D2 — Present only the boundary we actually enforce

The permission surface we stand behind is exactly the five hard gates in §2
(a–e). We will **stop presenting more than that**:

- Reframe the manifest **`capabilities:` block as disclosure, not enforcement.**
  It is author-declared transparency for the install review and the console — a
  statement of intent, never a runtime confinement. The console and docs must
  label it as such (e.g. "declared by the author — not enforced; a plugin runs
  with full privileges") so no operator mistakes a declared `network:` list for a
  network jail.
- **Whole-plugin enable/disable is the revocation primitive.** There is
  deliberately no per-capability runtime toggle (you cannot disable *just* a
  plugin's router or middleware); `tools.disabled` (by tool name) is the one
  finer lever and is retained. Disabling a route/surface/view plugin still needs
  a restart to fully unmount (FastAPI has no route-removal API) — documented, not
  fixed here.
- **Secrets section-scoping is documented as a convention, not a boundary.**

### D3 — Consent is provenance-based (formalize P1)

The meaningful control is **whose code this is**, gated at install/enable. Adopt
the provenance-and-consent model designed as P1:

- **`plugins.sources.official`** — glob list, default `["github.com/protoLabsAI/*"]`,
  **fork-overridable** so a fork (Roxy, Gina, protoTrader) points auto-trust at
  its own org via config, never a core edit (operator-fork contract).
- **`plugins.sources.acked`** — per-source globs the operator has consented to
  (written when they confirm a one-time dialog).
- **`plugins.trust_unverified`** — global "don't ask again" flag the dialog's
  checkbox flips.

Behavior:

- Installing/enabling from an **official** or **acked** source (or with
  `trust_unverified: true`) proceeds without a prompt.
- Any **other** source surfaces a **one-time consent**: *"This plugin runs code on
  your machine with your privileges. Install it only if you trust the source."*
  Confirming records an ack (per-source, or global via the checkbox).
- **Auto-enable-on-install (#899) is retained only behind this gate.** Trusted
  provenance ⇒ the one-click install-and-enable convenience stays. Unverified
  provenance ⇒ the consent gate is the thing standing between a URL and running
  code. This is what restores meaning to `install ≠ enable ≠ trust` (ADR 0027 D1)
  after #899 collapsed it: the three decisions are still separable, and consent is
  where "trust" is now actually recorded.
- **Auto-update (#1720) inherits source trust.** A plugin only auto-updates if it
  was installed from a source the operator trusts; pulling a newer commit from an
  already-trusted source needs no fresh consent (same trust decision, newer code
  from the same origin), consistent with pip/ComfyUI expectations.

The config-write path is load-bearing: `config_io.to_yaml` must serialize the new
`sources.*` fields or a saved ack is silently lost (the same class of bug that
dropped `plugins.disabled`/`sources.allow` before the 2026-06-10 audit). That is
Slice 1 of the follow-up.

### D4 — Scope of this ADR

Design-only. This ADR records the model and explicitly rejects the sandbox
alternatives. The **implementation** of D3 (config fields + persistence, the
official/acked gate, the consent dialog + ack API, the `capabilities:` relabel in
console/docs) is a tracked follow-up sliced as in the plugin-hardening plan
(foundation → gate → API → client → UI → docs). No runtime behavior changes when
this ADR merges.

## 4. Consequences

**Positive**

- The presented model matches the enforced model. An operator is told the honest
  thing — "plugins are trusted code; the control is *whose* code" — instead of a
  capability list that implies confinement we don't provide.
- Consent becomes a recorded, provenance-based decision, which is the
  precondition for unattended auto-update (#1720) to be sound.
- Forks retarget auto-trust by config, preserving the operator-fork contract.
- We stop paying the recurring cost of re-evaluating in-process sandboxes; the
  answer is written down: untrusted → MCP.

**Negative / accepted**

- A malicious *trusted-by-provenance* plugin has full machine access: filesystem,
  network, every plugin's secrets, internal APIs, `apply_settings`. This is
  **accepted** and mitigated by provenance + one-time consent + review, **not** by
  containment. Anyone needing containment uses MCP.
- The `capabilities:` relabel may read as a downgrade ("it used to look enforced").
  That perception *is* the bug being fixed.
- No per-capability revocation: pulling one misbehaving surface means disabling
  the whole plugin (+ restart for routes/surfaces).

## 5. Alternatives considered

- **Enforce the manifest `capabilities:` (network / fs / import allowlists) at
  runtime.** Rejected: unenforceable against a plugin's own Python without
  subprocess/OS isolation; monkeypatching `open`/`socket`/`import` is trivially
  escapable and would break legitimate library use. It would manufacture exactly
  the false confidence D2 removes.
- **OS-level sandbox** (seccomp / containers / namespaces per plugin). Rejected:
  protoAgent ships as a cross-platform desktop app (Tauri) + server; a portable
  OS sandbox is a large, brittle surface, kills the ComfyUI authoring DX, and
  still cannot scope *in-process* secret reads. Out of scope per ADR 0008.
- **Subprocess-per-plugin / WASM isolation.** Rejected as redundant: an
  out-of-process, capability-declared, sandboxable plugin **is** an MCP server.
  We already have that lane; we route untrusted code there rather than building a
  second isolation mechanism.
- **Signature / GPG verification of plugin releases.** Deferred: provenance via
  `source_url` + pinned `resolved_sha` + official-source globs is lighter and fits
  the git-URL model (ADR 0027 D2). Revisit if/when a first-party plugin *registry*
  lands (the deferred ADR 0001 Slice 5), where a signing authority would have a
  home.
- **Keep the status quo (no consent layer).** Rejected: auto-update (#1720) and
  auto-enable (#899) both assume a trust decision that is currently never
  recorded; the model should make that decision explicit.
