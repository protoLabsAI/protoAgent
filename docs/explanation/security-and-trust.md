# Security & the trust model

protoAgent's security posture is deliberately simple and **honest**: it sandboxes what is genuinely
untrusted, trusts what you deliberately installed, and tells you plainly that running arbitrary code
needs a container — it does **not** pretend otherwise. There are three actors, three postures.

## 1. Code you installed → trusted (by your choice)

A plugin's backend Python runs **in-process, with full access** — the filesystem and network as the
server user. This is intentional and it's the same deal as `pip install` / `npm install` / a VS Code
extension / a ComfyUI custom node: **installing a plugin runs its code, so installing it *is*
trusting it.** Only install plugins you'd trust with your shell.

Because the backend already runs as you, gating a plugin's *frontend* (its UI) behind extra "trust"
ceremony would be **theater** — it'd be harder on the browser-React of code whose Python you already
let run as your user. So plugin UIs are simply **sandboxed iframes** (the plugin serves its own page;
see [building a plugin view](../guides/building-react-plugin-views.md)) — uniform, no special "this
plugin is trusted" mode. (Earlier versions had a Module Federation in-process-React path behind a
trust gate; ADR 0038 retired it as inconsistent with this model.)

## 2. Code the agent generated → untrusted (always)

This is the boundary that genuinely matters. **Even with a trusted operator and trusted plugins, the
model's *output* is untrusted** — prompt injection, a hostile page it fetched, a hallucinated
snippet. So anything the agent *generates* and renders is sandboxed:

- **Artifacts** (`show_artifact` — generated HTML/SVG/Mermaid/React) render in a **nested
  `sandbox="allow-scripts"` iframe with no same-origin** — they run, but can't touch the console,
  its cookies, or its APIs. Same model as Claude Artifacts / Open WebUI.

This is the *one* place the sandbox isn't ceremony — it's the line between "code you installed" and
"code the model emitted."

## 3. The agent running code → opt-in, honestly partial

`execute_code` and the ACP coding-agent runtimes run real code. We don't pretend this is sandboxed:
`execute_code`'s own docs say it is *"isolation, not a true sandbox"* — a subprocess with a hard
timeout and a scrubbed env, but it can still touch the FS/network as the server user. The real
controls are:

- **Opt-in** — `execute_code` is off unless enabled.
- **Egress allowlist (ADR 0008)** — outbound HTTP is deny-by-default for private IPs, and a
  configured `egress.allowed_hosts` makes it a strict allowlist.
- **The honest guidance**: enable it for trusted-model output, or run inside a hardened container.

## The real isolation boundary is the container

Putting it together: if you want protection from genuinely **untrusted third-party plugins** (not
"I trust what I installed"), the lever is **not** any per-feature toggle — it's the **deployment
boundary**. Run protoAgent in a container (the recommended isolation boundary, see
[multi-instance](../guides/multi-instance.md)). Inside that boundary:

- installed plugins are trusted (you chose them),
- the agent's *generated* output is sandboxed (iframe),
- `execute_code` is opt-in and egress-gated.

That's the whole model, stated plainly: **trust what you install, sandbox what the model generates,
contain the blast radius.** No security theater, no pretending a plugin's React was more dangerous
than its Python.

## See also

- [Building a plugin view](../guides/building-react-plugin-views.md) — the sandboxed-iframe plugin UI model.
- ADR 0008 (egress allowlist), ADR 0027 (git-URL plugin install), ADR 0038 (why the in-process React
  trust gate was retired), ADR 0004 / [multi-instance](../guides/multi-instance.md) (containers as
  the isolation boundary).
