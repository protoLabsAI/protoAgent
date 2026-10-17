# Deploy on Proxmox (reusable LXC template)

Create a Debian + Docker template, then start and configure protoAgent separately
in each clone. This keeps agent data and operator tokens out of the template.
For a VM deployment, install Docker in the guest and follow
[Deploy in Docker](/guides/deploy-docker) directly.

## What you'll end up with

- A stopped template containing Docker and a pulled protoAgent image, with no
  agent container, volume, or credentials.
- Independent clones, each with its own operator token and `/sandbox` volume.

## Prerequisites

You need administrative access to a Proxmox node, an available Debian amd64
container template, outbound internet, and suitable storage and networking.
The example allocates 2 cores, 4 GB RAM, and 16 GB disk; adjust for your workload.

Docker inside unprivileged LXC depends on the host kernel, storage, and container
configuration. Check the [Proxmox container documentation](https://pve.proxmox.com/pve-docs/chapter-pct.html)
for your installed version. Use a VM if Docker cannot run under your host's
isolation policy; do not disable that policy just to complete this recipe.

## 1. Create the container

Run the following on the Proxmox node. Choose an unused ID, and replace the
storage and bridge names if your node uses different ones.

```bash
pveam update
pveam available --section system
# Copy a complete Debian amd64 archive filename from the list:
TEMPLATE=replace-with-the-listed-debian-archive.tar.zst
pveam download local "$TEMPLATE"
BASE=210
pct create "$BASE" "local:vztmpl/$TEMPLATE" \
  --hostname protoagent-base \
  --cores 2 --memory 4096 --swap 512 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 --ostype debian --onboot 0
pct start "$BASE"
```

## 2. Install Docker in the CT

Enter the container with `pct enter "$BASE"`, install Docker using the
[Docker Debian instructions](https://docs.docker.com/engine/install/debian/),
then exit back to the node. Verify that Docker can actually start a container:

```bash
pct exec "$BASE" -- docker run --rm hello-world
```

If that fails, inspect the guest's Docker logs and the host's LXC configuration
before proceeding. A running daemon alone does not establish that nested
containers work.

<span id="_3-install-protoagent-—-widen-the-bind"></span>
<span id="_4-finish-setup-in-the-browser"></span>

## 3. Prepare the image

Pull the image without running protoAgent. For repeatable clones, use your
chosen version or digest instead of `latest`.

```bash
pct exec "$BASE" -- docker pull ghcr.io/protolabsai/protoagent:latest
```

Do not create an agent container, finish setup, sign in to a model provider, or
install tailnet credentials in the base. Cloning copies guest files, including
Docker volumes and container environment variables; it does not reset them.

## 4. Convert the base to a template {#_5-convert-the-base-to-a-pve-template}

```bash
pct shutdown "$BASE"
pct template "$BASE"
```

Keep the base unchanged. To refresh it later, clone it to a scratch container,
update Docker and the pulled image, and convert that clean container into a new
template.

## 5. Clone and configure an instance {#_6-clone-a-working-instance-repeat-per-test}

Create a full clone so it has no runtime dependency on the base disk. Linked
clones are an alternative on storage that supports them.

```bash
CLONE=211
pct clone "$BASE" "$CLONE" --full 1 --hostname protoagent-test1
pct start "$CLONE"
```

Generate a **new token per clone** on the node and save the displayed value in
your credential manager. This command publishes port 7870 on every IPv4 interface
of the guest: restrict access with your firewall or private network before running it.

```bash
PROTOAGENT_CLONE_TOKEN=$(openssl rand -hex 24)
printf '%s\n' "$PROTOAGENT_CLONE_TOKEN"
pct exec "$CLONE" -- docker run -d --name protoagent \
  --restart unless-stopped --security-opt no-new-privileges:true \
  -e PROTOAGENT_UI=console \
  -e "A2A_AUTH_TOKEN=$PROTOAGENT_CLONE_TOKEN" \
  -p 0.0.0.0:7870:7870 -v protoagent-sandbox:/sandbox \
  ghcr.io/protolabsai/protoagent:latest
pct exec "$CLONE" -- ip -4 -o addr show eth0
```

Open `http://<clone-ip>:7870/app` over your private network, enter the token,
and finish model setup. `/healthz` should then return `200`. Before setup,
`/healthz` can return `503`; the authenticated setup-status endpoint distinguishes
an unconfigured agent from a failed start:

```bash
curl -H "Authorization: Bearer $PROTOAGENT_CLONE_TOKEN" \
  "http://<clone-ip>:7870/api/config/setup-status"
```

Replace `<clone-ip>` before running the command. For a seeded configuration,
use the [Docker seed pattern](/guides/deploy-docker#the-pattern) with each clone's
own volume and credentials.

## Day-2

| Task | Procedure |
| --- | --- |
| Update an instance | Back up, pull the chosen image, stop and remove only the `protoagent` container, then repeat its `docker run` command with the same volume and token. Let active turns finish first. |
| Back up | Follow [Docker backups](/guides/backup-and-restore#docker-backups), or stop the guest before a Proxmox backup. Include any external mounts separately; a rootfs backup does not necessarily include them. |
| Create a fresh test | Create another clone from the clean template and generate a new token. Retain the old instance until its data is no longer needed. |

## Reach it from anywhere (optional)

A `0.0.0.0` publish is not automatically LAN-only: routing and firewall rules
control who can reach it. Use [private remote access](/guides/phone-access) for
the console, or the [public A2A proxy pattern](/guides/exposing-protoagent) for
external agents. Register fleet peers by reachable address when multicast
mDNS discovery cannot cross the network boundary.
