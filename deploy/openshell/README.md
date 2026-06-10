# protoAgent under NVIDIA OpenShell

Run protoAgent (or a fork like Roxy) as an **OpenShell-managed sandbox** —
kernel-enforced filesystem (Landlock), per-binary deny-by-default network
egress (netns + proxy), and an unprivileged process identity — instead of the
app-level isolation alone. See
[ADR 0008](../../docs/adr/0008-sandboxing-and-openshell.md) and the
[Sandboxing & egress guide](../../docs/guides/sandboxing.md).

The model: a long-running **gateway** (control plane) provisions the agent as a
**sandbox** under a compute driver (docker / k8s). The sandbox's policy is
**generated from protoAgent's own config** — `filesystem.projects` → Landlock
paths, `egress.allowed_hosts` + `model.api_base` → the egress allowlist.

> **Validated:** the Docker path below was run end-to-end against OpenShell
> **v0.0.59** (gateway image + CLI) on 2026-06-10 — sandbox `Ready`, agent
> serving A2A from inside, off-policy egress blocked with `403`, off-policy
> writes denied by Landlock. OpenShell is pre-1.0; re-verify on upgrade.
> The Kubernetes section is still a starting template (not yet validated).

## Local / single host (Docker) — validated walkthrough

### 0. Prereqs

- Docker, and an agent image built from this repo: `docker build -t protoagent:local .`
  The Dockerfile ships **iproute2** — the OpenShell supervisor shells out to
  `ip` to build the egress network namespace and sandbox creation fails
  without it. Images built before this was added need a rebuild.
- The `openshell` CLI ([releases](https://github.com/NVIDIA/OpenShell/releases)
  ship static tarballs, deb/rpm, and an `install.sh`). Verify the checksum.

### 1. Gateway setup (one-time)

```bash
cd deploy/openshell

# Sandbox-JWT keypair — the docker driver refuses to start sandboxes without
# it ("docker sandboxes require gateway JWT auth"):
mkdir -p jwt && openssl genpkey -algorithm ed25519 -out jwt/signing.pem \
  && openssl pkey -in jwt/signing.pem -pubout -out jwt/public.pem \
  && echo "local-dev-key-1" > jwt/kid

# Gateway state dir — must be an ABSOLUTE path that exists on the host; it is
# mounted into the gateway container at the same path (the supervisor binary
# is extracted here and bind-mounted into sandboxes by the host docker daemon):
export OPENSHELL_STATE_DIR=/srv/openshell-state   # pick yours
sudo mkdir -p "$OPENSHELL_STATE_DIR" && sudo chown 1000:1000 "$OPENSHELL_STATE_DIR"

export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
export DOCKER_BRIDGE_IP=$(ip -4 addr show docker0 | awk '/inet /{sub(/\/.*/,"",$2); print $2}')

docker compose up -d
openshell gateway add http://127.0.0.1:8080 --local --name local
```

**Firewall:** sandboxes reach the gateway at
`http://host.openshell.internal:<port>` (the bridge IP). On hosts with a
default-DROP INPUT policy (ufw etc.), allow the OpenShell bridge in, e.g.:

```bash
BR="br-$(docker network inspect openshell-docker --format '{{.Id}}' | cut -c1-12)"
sudo iptables -I INPUT -i "$BR" -p tcp --dport 8080 -j ACCEPT
```

(Symptom otherwise: the sandbox crash-loops with `Policy fetch failed`.)

If host port 8080 is taken, set `OPENSHELL_GATEWAY_PORT` — the gateway
advertises its **bind** port to sandboxes, so compose keeps the container and
host ports identical.

### 2. Create the protoAgent sandbox

```bash
# PROTOAGENT_IMAGE / PROTOAGENT_PORT / PROTOAGENT_CONFIG override the defaults
PROTOAGENT_IMAGE=protoagent:local bash deploy/openshell/create-protoagent-sandbox.sh
```

The generated `openshell-policy.yaml` is the fence: the sandbox can only
read/write the project paths in the policy, and only the policy's binaries can
reach the policy's hosts. The agent's port is forwarded to
`127.0.0.1:<PROTOAGENT_PORT>` while the script runs (Ctrl-C stops the agent;
wrap in tmux/systemd for long-running use).

### 3. Verify

```bash
openshell sandbox list                       # PHASE should be Ready
curl http://127.0.0.1:7870/healthz           # via the forward

# the fence is real:
openshell sandbox exec --no-tty -- curl -s -m 8 https://example.com   # blocked (proxy 403)
openshell sandbox exec --no-tty -- touch /opt/protoagent/x            # blocked (Landlock)
```

### Gotchas (each cost us a debug loop)

| Symptom | Cause / fix |
|---|---|
| gateway crash-loop: `unable to open database file` | sqlx needs `?mode=rwc` to create the sqlite file **and** a writable state dir (gateway runs as uid 1000). compose.yml handles both; chown the state dir. |
| gateway crash-loop: `failed to create docker supervisor cache dir '/.local/share/...'` | distroless image has no `HOME`; compose.yml points `HOME`/`XDG_DATA_HOME` at the state dir. |
| `exec: "/opt/openshell/bin/openshell-sandbox": is a directory` | gateway-in-container extracted the supervisor to a path that doesn't exist on the host. The state dir must be an identical-path bind mount (compose.yml enforces this). |
| `docker sandboxes require gateway JWT auth` | generate the `jwt/` keypair (step 1). |
| CLI: `missing authorization header` | enabling gateway JWTs turns on auth for CLI calls too; `gateway.toml` sets `allow_unauthenticated_users = true` (local single-player only). |
| sandbox crash-loop: `Policy fetch failed` / `h2 protocol error` | supervisor can't reach the gateway: published port ≠ bind port, wrong bridge IP (`DOCKER_BRIDGE_IP` — custom `bip` daemons aren't 172.17.0.1), or firewall DROP (see above). An `h2 protocol error` usually means it connected to some *other* service squatting the port. |
| `Network namespace creation failed ... ip helper not found` | sandbox **image** must ship iproute2 (this repo's Dockerfile does). |
| `No module named server` | the supervisor does not inherit image ENV; pass `--env PYTHONPATH=/opt/protoagent` (the script does). |

## Kubernetes (starting template — not yet validated)

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
| `compose.yml` | OpenShell gateway as a container (docker driver) — validated v0.0.59 |
| `gateway.toml` | Gateway config: docker driver, sandbox JWTs, local-dev auth |
| `create-protoagent-sandbox.sh` | Generate the policy + create the protoAgent sandbox under the gateway |
| `k8s/values.yaml` | Helm values for the gateway (kubernetes driver) |
| `k8s/protoagent-sandbox.yaml` | Templated Agent-Sandbox CRD for protoAgent + policy ConfigMap wiring |

## GPUs

The protoAgent image itself is CPU-only, but OpenShell can grant a sandbox GPU
access at create time: `openshell sandbox create --gpu` (optionally
`--gpu-device nvidia.com/gpu=0`, CDI id) with the nvidia-container-toolkit on
the host. Pair with a CUDA-based agent image if the agent itself needs CUDA.

## Roxy

A read-only monitor (every project `write:false`) is the ideal tenant: the
policy makes her read-only authority **kernel-enforced** and pins egress to the
gateway + `gh`/git hosts. Generate her policy the same way
(`gen_openshell_policy.py` against Roxy's config) and run her sandbox with
`--env PROTOAGENT_INSTANCE=roxy`.
