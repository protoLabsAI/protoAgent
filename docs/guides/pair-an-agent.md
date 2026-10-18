# Pair a remote fleet member

Use this procedure to connect a hub to an agent on another machine. First [make the remote reachable](/guides/pairing#before-you-pair-make-this-agent-reachable). Agent pairing codes last five minutes and are single-use; restarting clears pending codes. Five wrong guesses cancel pending codes; generate a fresh code before retrying. To revoke the hub’s access, remove its entry on the remote under Settings → Devices. Removing a fleet row on the hub alone does not revoke its token.

## Pair another agent

A **hub** (the agent whose console you drive a fleet from) adds a protoAgent on another
machine as a [remote fleet member](/guides/fleet.md#remote-fleet-members-the-agent-there-the-ui-here).
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

  On the hub, enter it under Settings ▸ Fleet ▸ Discover ▸ Pair…, or run:
    protoagent fleet pair http://100.64.1.2:7870 K7QM2-XPA4F   (tailnet)
    protoagent fleet pair http://192.168.1.20:7870 K7QM2-XPA4F   (lan)
  ```

  A loopback-bound instance can't be paired; `protoagent pair` says so and points at
  [the reachability step](/guides/pairing#before-you-pair-make-this-agent-reachable).

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
[`delegate_to`](/guides/delegates.md) without holding its token. **Add as delegate** on the remote's
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
[Fleet](/guides/fleet.md#remote-fleet-members-the-agent-there-the-ui-here) for the rest of what a
remote member can do, including live views (terminal, agent_browser) over the hub.
