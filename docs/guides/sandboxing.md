# Sandboxing & egress

protoAgent's built-in isolation is **application-level**, and it's honest about
it ([ADR 0008](/adr/0008-sandboxing-and-openshell)). For real OS-enforced
isolation — kernel-level filesystem locking, syscall filtering, and
deny-by-default network egress — run protoAgent **under NVIDIA OpenShell**. This
guide covers both layers.

## What protoAgent enforces on its own

| Control | Enforcement | Strength |
|---|---|---|
| `execute_code` | subprocess + scrubbed env (no secrets) + hard timeout | isolation, **not a true sandbox** (its own docstring) |
| `fs_tools` fence (ADR 0007) | `resolve_project_path` containment in Python | **advisory** — same-process escape hatches run as the server user |
| Egress allowlist | `egress.allowed_hosts` enforced in `fetch_url` | real for `fetch_url`; **does not** fence `execute_code`/`run_command` egress |

These are useful defense-in-depth, but they do **not** replace OS isolation for
untrusted-model output. Treat `execute_code`/`run_command`/write-enabled
`fs_tools` as powerful — enable them only for trusted models **or under
OpenShell**.

## Layer 1 — the native egress allowlist (deny-by-default)

`fetch_url` is the tool where the model picks an arbitrary host — the main
in-process exfiltration / SSRF vector. Gate it with an allowlist:

```yaml
egress:
  allowed_hosts:
    - api.proto-labs.ai      # the model gateway
    - "*.github.com"         # gh / API (wildcard matches subdomains + apex)
    - docs.example.com
```

- **Empty list = permissive** (off) — existing deployments are unchanged until
  they opt in.
- When set, `fetch_url` **denies any host not on the list** with a clear error.
- `*.host` matches the apex and any subdomain; matching is case-insensitive and
  port-agnostic.
- Hot-reloads with the config (no restart).

> This covers the model-chosen-host vector. `web_search` / peers / MCP hit fixed
> configured endpoints; `execute_code` / `run_command` can still open sockets as
> the server user — those are only truly fenced by **Layer 2**.

## Layer 2 — run under NVIDIA OpenShell (OS-enforced)

[OpenShell](https://github.com/NVIDIA/OpenShell) runs an agent in a per-agent
container with a declarative, default-deny policy across four domains, enforced
at the OS boundary: **filesystem** (Landlock), **process** (seccomp),
**network** (netns + an OPA egress proxy), and **inference** (gateway routing +
credential stripping). It is, almost exactly, the *"hardened container"* our
`execute_code` docstring tells you to run inside.

### Generate a policy from your config

protoAgent generates a least-privilege starter policy from your **own config** —
the project registry becomes the Landlock paths, `egress.allowed_hosts` + the
model gateway become the network allowlist:

```bash
python scripts/gen_openshell_policy.py --config config/langgraph-config.yaml --out openshell-policy.yaml
```

The output maps directly (OpenShell **v1 policy schema**, validated against
v0.0.59):

- `filesystem.projects` → `filesystem_policy.read_only` / `read_write` (a
  `write:false` project becomes a **kernel-enforced** read-only path — so a
  monitor like Roxy *cannot* write even if something tried), plus an OS
  baseline under `landlock.compatibility: best_effort`;
- `egress.allowed_hosts` + `model.api_base` → `network_policies` endpoints,
  scoped to the agent's binaries (everything else denied by the per-sandbox
  proxy);
- `process.run_as_user: sandbox` → the unprivileged image user (root is
  rejected by OpenShell).

> OpenShell is pre-1.0 — re-verify against your installed release when
> upgrading (`openshell policy prove` can check properties of the output).
> It's a generated starting point, derived from real config, not a guess.

### Run it

```bash
# install OpenShell (see its docs), then wrap the agent's image/command:
openshell sandbox create --policy openshell-policy.yaml --from protoagent:local \
  --env PYTHONPATH=/opt/protoagent -- python -m server
```

Credentials are injected as env at runtime (never on disk); egress is
deny-by-default through the proxy; the filesystem is locked to the policy paths.

### Ready-made deployment

`deploy/openshell/` has a managed example end-to-end:

- **Docker:** `compose.yml` (the OpenShell gateway) + `create-protoagent-sandbox.sh`
  (generates the policy, creates the protoAgent sandbox under the gateway).
- **Kubernetes:** `k8s/values.yaml` (Helm gateway, kubernetes driver) +
  `k8s/protoagent-sandbox.yaml` (Agent-Sandbox CRD + policy ConfigMap) — after
  installing the Agent Sandbox CRDs.

See [`deploy/openshell/README.md`](https://github.com/protoLabsAI/protoAgent/blob/main/deploy/openshell/README.md). The Docker path is
**validated end-to-end against OpenShell v0.0.59** (including a gotcha table
from the validation run); the k8s CRD wiring is still a starting template
(OpenShell is pre-1.0 — verify fields against your release).

## Recommended posture

- **Trusted model, no code execution:** native egress allowlist is a sensible
  baseline; `execute_code`/write-`fs_tools` off.
- **Any `execute_code` / write-enabled / multi-project deployment (incl. Roxy):**
  run **under OpenShell** with the generated policy. The native egress allowlist
  still applies inside as defense-in-depth.

See [ADR 0008](/adr/0008-sandboxing-and-openshell) for the full rationale and the
[operator-fork guide](/guides/operator-fork) for Roxy (a read-only monitor is an
ideal OpenShell tenant).
