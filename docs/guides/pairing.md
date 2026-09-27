# Pair devices and agents

Pairing gives a phone, a tablet or another protoAgent **its own token** for this agent,
without you typing or pasting the shared operator bearer anywhere. The agent shows a
short-lived, single-use code; the other side claims it; the claim mints a per-client token
that this agent stores only as a hash and that you can revoke on its own, from
**Settings ▸ Devices**, without signing anything else out.

Two kinds of client pair this way:

| Pairing… | The code is | Claimed by | Decided in |
|---|---|---|---|
| **a phone or tablet** | a QR (a 32-character code in the link's `#fragment`), valid **2 minutes** | the phone's browser, by scanning | [ADR 0087](../adr/0087-device-pairing-and-per-device-tokens.md) |
| **another agent** (a hub adding this one as a remote member) | `XXXXX-XXXXX`, 10 characters, valid **5 minutes** | the hub's server, when you type the code there | [ADR 0113](../adr/0113-agent-pairing-for-remote-fleet-members.md) |

Both kinds are single-use and live in memory only, so a restart drops every pending code.
Five wrong guesses at an agent cancel every pending code; mint a new one and carry on.

## Before you pair: make this agent reachable

A phone or a hub on another machine can't reach an agent bound to loopback (`127.0.0.1`),
which is the default for a desktop or local run. If you open **Settings ▸ Devices ▸ Add a
device** (or *Pair an agent*) on a loopback-bound agent, the panel says so and offers
**Allow devices on my network**, listing the addresses it could be reached on. Tailnet
addresses come first and are marked as the safer pick: only your own devices can reach a
tailnet address, from any network, while a Wi-Fi (LAN) address is reachable by anything on
that network.

Picking an address does two things:

1. **It makes sure the agent has a token.** A non-loopback bind with no token refuses to
   start, so if this agent has none, one is generated, saved as its `auth.token`, and shown
   **once**. Save it: the desktop app after the restart, the CLI and any other browser will
   ask for it. (If the agent already has a token, nothing changes.)
2. **It sets the bind interface (`network.bind`) to `0.0.0.0`**, not to the address you
   picked. The server listens on a single host, and a single non-loopback address would
   drop loopback, which is how the desktop app reaches its own server. `0.0.0.0` keeps
   loopback and adds every other interface; the address you picked is only the one put in
   the QR or next to the code. The token is what gates access, and a tailnet ACL or a
   firewall is how you narrow reach further.

The bind only takes effect at startup, so the panel ends on **Restart protoAgent to
finish**. After the restart, open Settings ▸ Devices again and pair.

**To undo it**, set **Bind interface** back to `127.0.0.1` and restart. It is one of the
box-runtime knobs on **Settings ▸ Fleet** (the command palette finds it under `network`);
in YAML it is `network.bind` in the host config. The
agent is loopback-only again. The token stays; clear or rotate it under **Settings ▸
Operator & access** if you no longer want one. Revoke any paired devices you don't need
first, since they stop being reachable anyway.

On a headless server, skip the panel and start it reachable, with a token:

```bash
A2A_AUTH_TOKEN=$(openssl rand -hex 24) python -m server --host 0.0.0.0 --port 7870
```

Never turn on `PROTOAGENT_ALLOW_OPEN=1` to make pairing work. Pairing adds clients to a
*secured* instance; it is not a way to open one up.

## Pair a phone (QR)

1. On the agent: **Settings ▸ Devices ▸ Add a device**. A QR appears with a countdown, and
   a choice of address if the agent has more than one (Tailnet works from any network the
   phone is on; Wi-Fi only while both are on that Wi-Fi).
2. On the phone: scan it. The link opens the console at `…/app/#pair=<code>`; the console
   claims the code, stores the device token in that browser, and strips the code from the
   address bar and history. The code travels in the `#fragment`, so it never reaches a
   server log.
3. The device appears in the Devices list on the agent. That list growing is the
   confirmation; the phone can't tell the other window it succeeded.

To install the console on the phone's home screen and for the Tailscale setup, see
[Access from your phone](./phone-access.md).

## Pair another agent

A **hub** (the agent whose console you drive a fleet from) adds a protoAgent on another
machine as a [remote fleet member](./fleet.md#remote-fleet-members-the-agent-there-the-ui-here).
Pairing is how the hub gets a credential for it: the **remote** shows a code, the **hub**
claims it.

**1. Get a code on the remote.**

- With a console: **Settings ▸ Devices ▸ Pair an agent**. It shows the code, a countdown,
  and the base URLs the remote is reachable on, tailnet first.
- Headless (docker, a server): run `protoagent pair` on that machine. It finds the running
  instance, mints a code and prints a ready-made claim command per address:

  ```bash
  $ protoagent pair
  Pairing code for ava:  K7QM2-XPA4F   (expires in 4:59)

  On the hub, enter it under Settings ▸ Agents ▸ Pair…, or run:
    protoagent fleet pair http://100.64.1.2:7870 K7QM2-XPA4F   (tailnet)
    protoagent fleet pair http://192.168.1.20:7870 K7QM2-XPA4F   (lan)
  ```

  A loopback-bound instance can't be paired; `protoagent pair` says so and points at
  [the reachability step](#before-you-pair-make-this-agent-reachable).

**2. Claim it on the hub.**

- In the console: the fleet manager (**Settings ▸ Fleet**) → **Discover**, then **Pair…** on
  the remote's row. For a remote the scan can't see, use **Pair by URL…**. Type the code
  (case doesn't matter, dashes and spaces are ignored, and `O`/`I`/`L` read as `0`/`1`),
  and optionally a name.
- From a terminal:

  ```bash
  protoagent fleet pair http://100.64.1.2:7870 K7QM2-XPA4F [--name ava]
  # omit the code to be prompted, or pipe it with --code-stdin, to keep it out of shell history
  ```

- Or the API: `POST /api/fleet/remotes/pair` with `{"url": "…", "code": "…"}` (and
  optionally `name`, `allow_insecure`).

The hub redeems the code against the remote's `POST /api/pairing/claim` and stores the token
it gets back as that member's bearer (in its `remotes.json`, `0600`, never returned by the
API). The remote lists the hub in its Devices with an **Agent** badge, named
`<hub's agent name> (fleet hub)`. Pairing a URL that is already a member **replaces** its token
in place: that is the re-pair after a revoke. A wrong or expired code is a `400`; a remote
that is unreachable or doesn't answer like a protoAgent that supports pairing is a `502`.

## Plaintext: tailnet yes, LAN only if you say so

A pairing code, and the operator-tier token it turns into, would be readable by anyone on
the network if sent over plain `http://`. So the hub sends them over plain `http://`
without asking only to:

- **loopback**;
- **a tailnet address**: `100.64.0.0/10`, Tailscale's IPv6 range, or a `*.ts.net` MagicDNS
  name. A tailnet link is WireGuard-encrypted underneath.

`https://` is always fine. Plain `http://` to anything else, typically a LAN address like
`192.168.1.20`, is **refused** (`400`) with a message naming the fix: use the remote's
tailnet address or `https://`. If you trust the network, opt in explicitly: tick **I trust
this network — send it unencrypted** in the Pair dialog, pass `--insecure-http` to
`protoagent fleet pair`, or `allow_insecure: true` on the API. A hostname is judged by every
address it resolves to at pairing time. The same rule applies when a token is stored by "Add
a remote by URL" or an edit. The opt-in doesn't make a LAN safe; it makes it a decision you
take knowingly. TLS on the remote is the real fix.

## Revoke

On the agent that was paired **to**: **Settings ▸ Devices**, then the trash icon on the
phone's or hub's row. The token stops working at once, and nothing else is signed out: the
shared bearer, the desktop app and every other device keep working.

- A revoked **phone** is asked for a token again on its next request.
- A revoked **hub** sees its next proxied call to that remote answer `401`, and its fleet
  row turns to **token rejected — re-pair** within about 30 seconds (the next token check).
  Get a new code on the remote and **Re-pair** from the hub's row.

Removing the remote from the hub's fleet only unregisters it on the hub; revoke it on the
remote too if the hub shouldn't hold a working token.

## The `auth` badge

A remote member with a stored token is checked with that token every 30 seconds, and at once
after an add, edit or pair. The fleet manager shows the result on the row:

| `auth` | Badge | Meaning |
|---|---|---|
| `ok` | *paired* | The remote requires auth and the token passes. |
| `rejected` | *token rejected — re-pair* | `401`/`403` with the token: it was revoked or is wrong. |
| `open` | *open — no token needed* | The remote answers without any token: its auth is off, so anyone who can reach it can drive it. |
| `none` | *not paired* | No token is stored. |
| `unknown` | (none) | No verdict yet, or an older remote without the check route. |

The same field is on each remote in `GET /api/fleet` and `protoagent fleet ls --json`.

## Delegate to a paired remote

Once a remote is paired, your agents can call it with
[`delegate_to`](./delegates.md) without holding its token. **Add as delegate** on the remote's
fleet row points the delegate at the **hub's own loopback proxy**,
`http://127.0.0.1:<hub-port>/agents/<remote-id>/a2a`, with no token. On loopback the delegate
presents the fleet service token, the hub accepts it, and the proxy swaps in the remote's
stored token. So the paired token lives in exactly one place, the hub's fleet row, and
re-pairing fixes the window and every delegate at once. It never leaves the hub's machine.

That URL resolves on the hub's machine, so the button writes it only for the hub and its
local members; in a remote member's own window it is disabled. A remote that was registered
with **no** token is proxied with no credential, so a secured one answers `401` and the
delegate's error tells you to pair it. The hub has to be up for these delegates to work, as
it already is for the member itself. See
[Fleet](./fleet.md#remote-fleet-members-the-agent-there-the-ui-here) for the rest of what a
remote member can do, including live views (terminal, agent_browser) over the hub.
