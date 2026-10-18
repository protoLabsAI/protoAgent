# Pair devices and agents

Pairing gives a phone, a tablet or another protoAgent **its own token** for this agent,
without you typing or pasting the shared operator bearer anywhere. The agent shows a
short-lived, single-use code; the other side claims it; the claim mints a per-client token
that this agent stores only as a hash and that you can revoke on its own, from
**Settings ▸ Devices**, without signing anything else out.

For a phone or tablet, scan a QR code from **Settings → Devices → Add a device**.
The code expires after two minutes and is single-use; restarting clears pending
codes. The resulting token grants operator access to this agent, so pair only
devices you intend to give that control.

To add another agent to a fleet, use [Pair a remote fleet member](/guides/pair-an-agent).

## Before you pair: make this agent reachable

A phone or a hub on another machine can't reach an agent bound to loopback (`127.0.0.1`),
which is the default for a desktop or local run. Open **Settings ▸ Devices ▸ Add a
device** (or **Pair an agent**). If the agent is loopback-bound, the reachability
notice lists addresses; choose **Tailnet** or **Wi-Fi** to allow access. Tailnet
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
PROTOAGENT_PAIR_TOKEN=$(openssl rand -hex 24)
printf '%s\n' "$PROTOAGENT_PAIR_TOKEN"
A2A_AUTH_TOKEN="$PROTOAGENT_PAIR_TOKEN" protoagent serve --host 0.0.0.0 --port 7870
```

Keep the displayed token for any client that needs the shared operator login.
In a source checkout, replace `protoagent serve` with `uv run python -m server`.

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

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.

## Plaintext: tailnet yes, LAN only if you say so

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.

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

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.

## Delegate to a paired remote

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.
