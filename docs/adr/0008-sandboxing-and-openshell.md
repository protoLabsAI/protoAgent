# ADR 0008 — Sandboxing posture & NVIDIA OpenShell

- **Status:** Accepted (2026-06-01) — native egress allowlist + OpenShell policy generator + guide shipping alongside
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** security, sandboxing, isolation, egress, openshell, execute_code, filesystem
- **Supersedes / Superseded by:** —

> Accepted. protoAgent's isolation today is **application-level, not
> OS-enforced**: `execute_code` is a scrubbed-env subprocess (its own docstring
> says *"isolation, not a true sandbox"*), the `tools/fs_tools.py` fence (ADR
> 0007) is an in-process path check, `run_command` runs as the server user, and
> there is **no network-egress control at all**. NVIDIA **OpenShell** is, almost
> line-for-line, the *"hardened container"* our `execute_code` docstring already
> tells operators to run inside — kernel-enforced filesystem (Landlock),
> syscall filtering (seccomp), and **deny-by-default egress** (netns + an OPA
> proxy). The decision: **layer OS-level isolation by supporting protoAgent
> running *under* OpenShell** (a policy generated from protoAgent's own config —
> the project registry → Landlock paths, an egress allowlist → the network
> policy) **and** adopt the cheapest, highest-impact lesson **natively** — a
> deny-by-default **egress allowlist** enforced in `fetch_url`. One source of
> truth (`egress.allowed_hosts` + the project registry) feeds both layers.

---

## 1. Context & Problem Statement

An agent that runs model-authored code (`execute_code`), shells out
(`run_command`), and writes files (`fs_tools`, ADR 0007) is a real attack
surface. Current isolation is **app-level only**:

| Surface | Today | Gap |
|---|---|---|
| `execute_code` | subprocess + scrubbed env (PATH + bridge FDs, no secrets) + timeout | no seccomp, no fs lock, **no network limit** — "isolation, not a true sandbox" (its docstring) |
| `fs_tools` fence (ADR 0007) | `resolve_project_path` containment in Python | **advisory** — same-process `run_command`/`execute_code` runs as the server user and can step outside it |
| `run_command` | arbitrary argv as server user | no syscall/network restriction |
| **network egress** | none | `fetch_url` (model-chosen host), `web_search`, peers, MCP, `execute_code` can reach anything — **exfiltration risk** |
| audit | app-level JSONL + Langfuse | not kernel-level, not OCSF |

**NVIDIA OpenShell** (`openshell sandbox create -- <cmd>`) runs an agent in a
per-agent container with a declarative, **default-deny** policy over four
domains, enforced at the OS boundary (Landlock + seccomp + netns, applied by a
supervisor after fork, before exec):

| Domain | Mechanism | Locked? |
|---|---|---|
| Filesystem | **Landlock LSM** — allowed paths only | at creation |
| Process | **seccomp-BPF** — blocks ptrace/mount/pivot_root/clone+unshare/raw sockets | at creation |
| Network | **netns + HTTP CONNECT proxy (OPA/Rego)** — egress by method+path | hot-reload |
| Inference | proxy — reroute model calls, **strip caller creds** | hot-reload |

A long-running gateway manages lifecycle; drivers are docker/podman/**microVM**/
k8s; creds are injected as env (never on disk); audit is **OCSF JSON**. It is
built to run the **whole agent inside** (not to delegate a single subprocess
out), which suits us — we already ship a container.

## 2. Decision

**Layer two complementary controls; don't reinvent the kernel parts.**

1. **Support running protoAgent *under* OpenShell (the strong layer).** We
   already containerize; wrap the image in an OpenShell sandbox with a policy
   **generated from protoAgent's own config**:
   - *filesystem* allowed-paths = the `filesystem.projects` roots (a project's
     `write:false` → read-only Landlock — so Roxy's read-only authority becomes
     **kernel-enforced**, not just persona-enforced) + the data root.
   - *network* allowlist = `egress.allowed_hosts` + the model `api_base` +
     known fleet endpoints — deny everything else.
   - *inference* pinned to the gateway; *process* = default seccomp → giving
     `execute_code` real syscall filtering.
   This closes the `execute_code` "not a true sandbox" gap and adds egress
   control with **config, not code**.

2. **Native egress allowlist (the defense-in-depth layer).** Add
   `egress.allowed_hosts`; enforce **deny-by-default** in `fetch_url` (the tool
   where the model picks an arbitrary host — the main in-process exfil/SSRF
   vector). Empty list = permissive (today's behavior); set = only those hosts.
   This works **with or without** OpenShell and reuses the existing
   `PUSH_NOTIFICATION_ALLOWED_HOSTS` SSRF-guard pattern.

3. **One source of truth.** `egress.allowed_hosts` feeds *both* `fetch_url`
   enforcement (in-process) *and* the generated OpenShell network policy
   (process-level); the project registry feeds the Landlock paths. Configure
   once, enforced at both layers.

## 3. What ships with this ADR

- **`egress.py`** + `config.egress_allowed_hosts` + enforcement in `fetch_url`
  (deny-by-default when set; `*.example.com` subdomain wildcards supported).
- **`scripts/gen_openshell_policy.py`** — reads `langgraph-config.yaml`, emits a
  starter OpenShell policy (filesystem paths from the project registry, network
  allowlist from `egress.allowed_hosts` + `model.api_base`, process seccomp,
  inference→gateway). A *generated starting point* in OpenShell's documented
  4-domain shape — field names may need a tweak for the installed release.
- **`docs/guides/sandboxing.md`** — the threat model, "run under OpenShell" with
  the generator, and the native egress allowlist.

## 4. Security model

- **Two layers, not one.** Native egress allowlist mediates the tools we control
  (`fetch_url`); OpenShell mediates the *process* (subprocess escapes via
  `execute_code`/`run_command`, raw sockets, fs). Neither alone is complete;
  together they cover model-chosen URLs **and** the subprocess escape hatch.
- **Deny-by-default** once configured; empty allowlist stays permissive so
  existing deployments are unchanged until they opt in.
- **The fs fence is honest about its level** — advisory in-process (ADR 0007),
  kernel-enforced only under OpenShell's Landlock. Documented as such.
- The generated policy is **least-privilege from real config**, not hand-rolled
  guesses — the paths/hosts come from what the agent is actually configured to use.

## 5. Consequences

**Positive**
- A real OS-enforced isolation story for `execute_code`/fs/egress, with config
  not code; the `execute_code` docstring's "run inside a hardened container"
  caveat gets a concrete, supported answer.
- The single biggest gap (no egress control) gets a native, immediately-useful
  deny-by-default allowlist that doesn't require OpenShell.
- Roxy's read-only authority can be made kernel-enforced (defense-in-depth over
  her persona + the in-process fence).

**Negative / costs**
- OpenShell is Linux-first (Landlock/seccomp/netns); macOS/WSL run the container
  path with weaker host guarantees. The generated policy targets the documented
  schema and may need per-release field tweaks.
- Native egress only covers `fetch_url`; `web_search`/peers/MCP hit fixed
  endpoints and `execute_code`/`run_command` egress is only truly fenced under
  OpenShell (or a host firewall) — documented plainly.

## 6. Alternatives considered

- **Native seccomp + Landlock in protoAgent** (replicate OpenShell) — high
  effort, Linux-specific, and we'd be rebuilding what OpenShell does well.
  Rejected as primary; OpenShell is the OS-enforcement layer, we add only the
  cheap native egress control.
- **Per-subprocess delegation into OpenShell** (sandbox just `execute_code`) —
  OpenShell is built to run the whole agent inside, not to accept a single
  delegated subprocess; rejected in favor of running the agent under it.
- **No egress control / status quo** — rejected; it's the single biggest gap.

## 7. Open questions

- Exact OpenShell **policy YAML schema per release** — the generator targets the
  documented 4-domain model; validate against the installed version.
- Should the egress allowlist also gate `web_search`/peer/MCP target hosts, or
  leave those to their fixed config + the OpenShell netns layer? (Leaning: leave
  to OpenShell; `fetch_url` is the model-chosen-host vector.)
- ✅ A managed "protoAgent-under-OpenShell" compose/k8s example — shipped in
  `deploy/openshell/` (gateway compose + sandbox-create script; Helm values +
  Agent-Sandbox CRD template for k8s).

## 8. Related

- [ADR 0007 — Directory-Aware Operator Primitives](/adr/0007-directory-aware-operator-agent) — the project registry that feeds the Landlock filesystem paths; Roxy is the prime beneficiary.
- [ADR 0006 — Observability](/adr/0006-observability-and-the-self-improving-flywheel) — audit/telemetry complements OpenShell's OCSF logs.
- `tools/execute_code.py` (the "not a true sandbox" caveat), `tools/lg_tools.py`
  (`fetch_url`), `a2a_handler.py` (`PUSH_NOTIFICATION_ALLOWED_HOSTS` SSRF
  pattern), `graph/llm.py` (gateway egress).
- NVIDIA OpenShell: [docs](https://docs.nvidia.com/openshell/home), [repo](https://github.com/NVIDIA/OpenShell).
