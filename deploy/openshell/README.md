# protoAgent under NVIDIA OpenShell

Run protoAgent (or a fork like Roxy) as an **OpenShell-managed sandbox** —
kernel-enforced filesystem (Landlock), syscall filtering (seccomp), and
deny-by-default network egress — instead of the app-level isolation alone. See
[ADR 0008](../../docs/adr/0008-sandboxing-and-openshell.md) and the
[Sandboxing & egress guide](../../docs/guides/sandboxing.md).

The model: a long-running **gateway** (control plane) provisions the agent as a
**sandbox** under a compute driver (docker / k8s). The sandbox's policy is
**generated from protoAgent's own config** — `filesystem.projects` → Landlock
paths, `egress.allowed_hosts` + `model.api_base` → the egress allowlist.

> Verbatim gateway/Helm bits are from OpenShell's docs; the sandbox/CRD wiring is
> a starting template — OpenShell + the Agent Sandbox CRD are pre-1.0, so verify
> fields against the versions you install.

## Local / single host (Docker)

```bash
# 1) gateway (control plane)
docker compose -f deploy/openshell/compose.yml up -d
openshell gateway add http://127.0.0.1:8080 --local --name local

# 2) generate the policy + create the protoAgent sandbox
bash deploy/openshell/create-protoagent-sandbox.sh
#   PROTOAGENT_IMAGE / PROTOAGENT_PORT / PROTOAGENT_CONFIG override the defaults

openshell sandbox list
```

The generated `openshell-policy.yaml` is the fence: the sandbox can only read/
write the project paths in the policy and only reach the allowlisted hosts.

## Kubernetes

```bash
# 1) Agent Sandbox controller + CRDs (OpenShell builds on the SIG project)
VERSION=$(curl -s https://api.github.com/repos/kubernetes-sigs/agent-sandbox/releases/latest | jq -r .tag_name)
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$VERSION/manifest.yaml

# 2) the OpenShell gateway (StatefulSet + PKI + RBAC)
kubectl create namespace openshell
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  --version <release> --namespace openshell -f deploy/openshell/k8s/values.yaml

# 3) policy ConfigMap (generated from config) + the protoAgent sandbox
python scripts/gen_openshell_policy.py --config config/langgraph-config.yaml --out /tmp/policy.yaml
kubectl -n openshell create configmap protoagent-openshell-policy --from-file=policy.yaml=/tmp/policy.yaml
kubectl -n openshell create secret generic protoagent-config \
  --from-file=langgraph-config.yaml=config/langgraph-config.yaml
kubectl apply -f deploy/openshell/k8s/protoagent-sandbox.yaml
```

## Files

| File | What |
|---|---|
| `compose.yml` | OpenShell gateway as a container (docker driver) — verbatim from OpenShell docs |
| `create-protoagent-sandbox.sh` | Generate the policy + create the protoAgent sandbox under the gateway |
| `k8s/values.yaml` | Helm values for the gateway (kubernetes driver) |
| `k8s/protoagent-sandbox.yaml` | Templated Agent-Sandbox CRD for protoAgent + policy ConfigMap wiring |

## Roxy

A read-only monitor (every project `write:false`) is the ideal tenant: the
policy makes her read-only authority **kernel-enforced** and pins egress to the
gateway + `gh`/git hosts. Generate her policy the same way (`gen_openshell_policy.py`
against Roxy's config) and run her sandbox with `PROTOAGENT_INSTANCE=roxy`.
