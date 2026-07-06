# Deploy on Proxmox (reusable LXC template)

Run protoAgent on Proxmox VE as **Docker-in-LXC**, then bake that container into a
**PVE template** so you can `clone` a fresh, working agent in seconds — for a test
instance, a per-project agent, or a small fleet.

This uses the repo's own [`scripts/install.sh`](/guides/deploy-docker#one-command-install)
inside the CT — you keep the GHCR image, the update path, and the `/sandbox` data
volume. No bare-metal Python install, no building the console from source.

> **Why LXC and not a VM?** For a single lightweight FastAPI agent the container
> overhead savings are real, and Docker-in-LXC with `nesting` is a well-worn homelab
> path. A VM + Docker is the Proxmox-official answer for Docker workloads and is a
> touch more robust across PVE major upgrades — prefer it if you run many Docker
> workloads or want the strongest isolation. Everything below applies to a VM too;
> only steps 1–2 differ.

## What you'll end up with

- **CT `<base>`** — a stopped PVE *template*: Debian + Docker + the protoAgent image
  pulled and its container defined, but **unconfigured** (no model, no secrets).
- **CT `<clone>`, `<clone+1>`, …** — linked clones off the template. Each boots, Docker
  auto-starts the agent, and you finish model setup per-clone in the browser.

Linked clones share the template's base disk (copy-on-write), so each test instance
costs only its own delta — spinning up ten agents doesn't cost ten full disks.

## Prerequisites

- Proxmox VE 8 or 9, with a Debian 12 (or 13) LXC template downloaded
  (`pveam update && pveam download local debian-12-standard`).
- Outbound internet from the CT (to pull the Docker packages + GHCR image).
- ~16 GB disk and 2–4 GB RAM per instance. No GPU — model calls go out to your
  gateway (LiteLLM / OpenAI-compatible).

## 1. Create the container

Unprivileged, with `nesting` (Docker needs it) and `keyctl` (Docker's containerd
keyring). Use **DHCP** so every clone gets its own address without collisions.

```bash
# Pick an unused VMID for the base; the clones will take the next ones.
BASE=210

pct create $BASE local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst \
  --hostname protoagent \
  --cores 2 --memory 4096 --swap 512 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --ostype debian --onboot 0

pct start $BASE
```

> **The one Proxmox gotcha is the bind (step 3), not the CT.** Docker-in-unprivileged-LXC
> "just works" on current kernels with `nesting=1,keyctl=1`. If `dockerd` ever fails to
> start on an older kernel, install `fuse-overlayfs` in the CT and set
> `{"storage-driver":"fuse-overlayfs"}` in `/etc/docker/daemon.json`.

## 2. Install Docker in the CT

```bash
pct exec $BASE -- bash -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  curl -fsSL https://get.docker.com | sh
'
pct exec $BASE -- docker info >/dev/null && echo "docker OK"
```

## 3. Install protoAgent — widen the bind

`127.0.0.1:7870` is loopback **inside the CT** — useless from your LAN. To reach the
console from your workstation, publish on `0.0.0.0` and set an `A2A_AUTH_TOKEN` (the
boot gate **requires** the token for any non-loopback bind — see
[Binding & auth](/guides/deploy-docker#binding-auth)). Generate it once:

```bash
TOKEN=$(openssl rand -hex 24)
echo "$TOKEN"      # save this — you need it to sign in to the console

pct exec $BASE -- bash -c "
  export A2A_AUTH_TOKEN='$TOKEN'
  export PROTOAGENT_BIND=0.0.0.0
  export PROTOAGENT_INSTALL_NONINTERACTIVE=1
  curl -fsSL https://raw.githubusercontent.com/protoLabsAI/protoAgent/main/scripts/install.sh | sh
"
```

`PROTOAGENT_INSTALL_NONINTERACTIVE=1` starts the container without prompting — you'll
finish the model wizard in the browser (step 4). Prefer config-as-code over the wizard?
Bake a seed image instead (see [Deploy in Docker](/guides/deploy-docker#the-pattern))
and `docker run` that image in place of the installer.

> **Expected quirk — a false "did not come up in time."** When `A2A_AUTH_TOKEN` is set,
> the installer's readiness probe (`GET /api/config/setup-status`) is itself bearer-gated,
> so the *unauthenticated* probe gets `401` and the installer eventually prints
> **"protoAgent did not come up in time."** The agent is actually **running and healthy** —
> only the probe was denied. Confirm with the token:
>
> ```bash
> IP=$(pct exec $BASE -- ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
> curl -s -H "Authorization: Bearer $TOKEN" http://$IP:7870/api/config/setup-status
> # → {"setup_complete":false,"presets":[...]}   ← healthy, awaiting setup
> ```

## 4. Finish setup in the browser

Get the CT's IP and open the console:

```bash
pct exec $BASE -- ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1
# → e.g. 192.168.4.26
```

Open `http://<ct-ip>:7870/app`. The console prompts for the operator token — paste the
`A2A_AUTH_TOKEN` from step 3 (it's cached in that browser's `localStorage`; see
[Where the operator token lives](/guides/deploy-docker#where-the-operator-token-lives)).
Then run the setup wizard: gateway URL (your LiteLLM / OpenAI-compatible endpoint),
API key, and model.

**If this CT will be your template, stop here — don't run the wizard.** Configure the
clones instead (step 6), so the template stays a clean, credential-free base.

## 5. Convert the base to a PVE template

Stop the CT and template it. The rootfs becomes an immutable base that linked clones
reference — the same pattern PVE uses for any template CT.

```bash
pct stop $BASE
pct template $BASE          # renames the disk to base-<id>-disk-0
```

> Once templated, **never start or modify `<base>`** — linked clones depend on its base
> disk staying immutable.

## 6. Clone a working instance (repeat per test)

```bash
CLONE=211
pct clone $BASE $CLONE --hostname protoagent-test1
pct start $CLONE
```

On boot, Docker starts and the protoAgent container auto-restarts
(`--restart unless-stopped`) against its own fresh `/sandbox` volume — so the clone comes
up **unconfigured, awaiting setup**. Grab its IP and finish setup in the browser (step 4):

```bash
pct exec $CLONE -- ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1
```

> **The token is shared across clones** (it's baked into the container's env in the base
> image). Fine for a trusted test fleet. To give one clone its own token, rotate it inside
> that clone:
>
> ```bash
> pct exec $CLONE -- docker rm -f protoagent
> pct exec $CLONE -- bash -c "A2A_AUTH_TOKEN=$(openssl rand -hex 24) PROTOAGENT_BIND=0.0.0.0 \
>   PROTOAGENT_INSTALL_NONINTERACTIVE=1 \
>   sh -c 'curl -fsSL https://raw.githubusercontent.com/protoLabsAI/protoAgent/main/scripts/install.sh | sh'"
> ```
>
> The `protoagent-sandbox` volume (and its config) is preserved across that re-run.

## Day-2

| Want to… | Do |
| --- | --- |
| Update an instance | Re-run the installer in the CT (pulls latest image, keeps the volume), or run a `watchtower` container polling `latest`. |
| Back it up | `vzdump <clone>` — all agent state lives in the `protoagent-sandbox` Docker volume on the CT rootfs, so a CT backup **is** a complete backup. |
| Reset a test to zero | `docker rm -f protoagent && docker volume rm protoagent-sandbox` in the CT, then re-run the installer — or just destroy the clone and clone again. |
| Update the template itself | Clone the template to a scratch CT, start it, re-run the installer, stop it, and re-`pct template` a fresh base (templates can't be edited in place). |

## Reach it from anywhere (optional)

The wide bind above exposes the agent on your **LAN** only. For remote access without
opening a router port, add Tailscale to the CT (give it `/dev/net/tun` and run
`tailscale up --ssh`) — see [phone access](/guides/phone-access#tailscale-reach-it-from-anywhere) —
or front it with a Cloudflare tunnel (keep `A2A_AUTH_TOKEN` set and set `A2A_PUBLIC_URL`;
see [Expose it with a tunnel](/guides/deploy-docker#expose-it-with-a-tunnel-ngrok-cloudflare)).

> **mDNS/fleet discovery won't cross the Proxmox bridge** onto another subnet — register
> fleet members by tailnet name or direct IP rather than relying on mDNS.
