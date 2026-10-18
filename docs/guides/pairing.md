# Pair devices and agents

Pairing gives a phone, a tablet or another protoAgent **its own token** for this agent,
without you typing or pasting the shared operator bearer anywhere. The agent shows a
short-lived, single-use code; the other side claims it; the claim mints a per-client token
that this agent stores only as a hash and that you can revoke on its own, from
**Settings ▸ Devices**, without signing anything else out.

For a phone or tablet, open **Settings → Devices → Add a device** and either scan the
QR or type the short code shown beside it (`XXXXX-XXXXX`). On a box with no screen,
`protoagent pair --device` prints the same code and QR in the terminal. A code expires
after five minutes and works once; restarting clears pending codes. The resulting token
grants operator access to this agent, so pair only devices you intend to give that
control.

To add another agent to a fleet, use [Pair a remote fleet member](/guides/pair-an-agent).

## Before you pair: make this agent reachable

A phone or a hub on another machine can't reach an agent bound to loopback (`127.0.0.1`),
which is the default for a desktop or local run. Open **Settings ▸ Devices ▸ Add a
device** (or **Pair an agent**). If the agent is loopback-bound, the reachability
notice lists addresses; choose **Tailnet** or **Wi-Fi** to allow access. Tailnet
addresses come first. Access depends on your tailnet membership and ACLs; a LAN
address can be reachable by other devices on that network. Check the applicable
network rules before pairing.

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
3. The phone shows **Paired as &lt;name&gt;**, the name it now has in the agent's Devices list
   (a guess from the browser, such as `iPhone` or `Android phone`; you can
   [rename it](#rename-a-device)). On the agent, the device appears in the Devices list and
   a **Device paired** notice names it.

## Pair a phone by typing the code

Use this when the phone can't scan the QR (no camera access, a broken camera, the code
is on a terminal or a screen share), or when you'd rather not.

1. On the agent: **Settings ▸ Devices ▸ Add a device**. Beside the QR, the panel shows a
   ten-character code such as `3HV8T-QW6RN`, the console URL to open on the phone, and
   the countdown. On a headless box, run [`protoagent pair --device`](#pair-a-phone-from-a-headless-box)
   instead.
2. On the phone: open that URL in the browser, for example `http://100.64.0.5:7870/app/`.
   The console asks for authentication.
3. Tap **Have a pairing code?**, type the code and tap **Pair this device**. Case doesn't
   matter, the dash is optional, and the letters O, I and L are read as 0, 1 and 1, so a
   misread look-alike still works.
4. The console opens and shows **Paired as &lt;name&gt;**. The phone appears in
   **Settings ▸ Devices** on the agent with its own token.

The QR and the typed code are the same code. Using either one spends it.

### If the code doesn't work

The agent answers every bad code the same way, on purpose: it never tells a guesser which
codes exist. The phone shows **That code didn't work** and names the possible causes.

| Cause | What happened | What to do |
| --- | --- | --- |
| Mistyped | A character is wrong. The **Pair this device** button stays disabled until all ten characters are entered, so a short code never spends a try. | Check it against the agent's screen and try again. |
| Expired | Codes last five minutes. Restarting the agent, or closing the Add a device card, also cancels the code. | Make a new code. |
| Already used | Each code works once, by QR or by typing. | Make a new code if this phone isn't in the Devices list. |
| Locked out | After five wrong codes in a row, the agent cancels **every** pending code, including agent-pairing codes. This stops anyone from guessing their way in. | Make a new code. A new code starts with five fresh tries. |

If the phone says **Couldn't reach this agent**, the problem is the network, not the
code. See [Access from your phone](./phone-access.md#tailscale-reach-it-from-anywhere).

## Pair a phone from a headless box

A server or container with no screen can still pair a phone. Run this on the machine
where the agent is running:

```bash
protoagent pair --device
```

It finds the running instance the same way `protoagent pair` does, starts a phone
pairing, and prints a QR in the terminal, the code, and the console URL for each address
the phone can use (tailnet first). Scan the QR, or open a URL on the phone and type the
code as described above. `--no-qr` skips the drawing, `--light` draws it for a
light-background terminal, and `--json` prints the raw response. See the
[CLI reference](./cli.md#pairing-a-phone-from-the-terminal-protoagent-pair-device) for
example output.

The agent must serve the console (the default `--ui console`) and be reachable with a
token, as in [Before you pair](#before-you-pair-make-this-agent-reachable). On a
`--ui none` agent the command says to restart it with `--ui console` and exits 1, without
making a code. Pairing another agent doesn't need the console.

To install the console on the phone's home screen and for the Tailscale setup, see
[Access from your phone](./phone-access.md).

## Pair another agent

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.

## Plaintext: tailnet yes, LAN only if you say so

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.

## Manage devices

**Settings ▸ Devices** lists every phone, tablet and hub paired to this agent. Each row
shows the name, a **Device** or **Agent** badge, and when it was last used.

- **active** means the device made a request in the last few minutes. The agent records
  last use at most once every five minutes, to avoid a disk write on every request, so
  the badge covers that whole window and the row reads **in use in the last few minutes**
  rather than a misleading exact time.
- **This device** marks the row for the browser you're using, when that browser was
  paired. A browser signed in with the operator token isn't a paired device, so no row
  is marked.

### Rename a device

Select the pencil icon on its row, type a new name and press **Enter** (or **Save**).
**Escape** cancels. Names are 1 to 64 characters on one line. Control and invisible
formatting characters are refused, so a name can't be made to display as something else.
Renaming changes only the label: the device stays signed in.

A paired phone can rename its own row from its own Settings, which is the quickest way to
replace a guessed name like `iPhone`.

### Revoke

On the agent that was paired **to**: **Settings ▸ Devices**, then the trash icon on the
phone's or hub's row. The token stops working at once, and nothing else is signed out: the
shared bearer, the desktop app and every other device keep working.

- A revoked **phone** is asked for a token again on its next request.
- A revoked **hub** sees its next proxied call to that remote answer `401`, and its fleet
  row turns to **token rejected — re-pair** within about 30 seconds (the next token check).
  Get a new code on the remote and **Re-pair** from the hub's row.
- **Revoking this device** (the row marked **This device**) asks first, in the panel:
  **Remove and sign out** or **Keep**. Removing it forgets the device token in this
  browser. If the browser also holds the operator token, you stay signed in with that;
  otherwise the console asks you to pair again or enter a token.

Removing the remote from the hub's fleet only unregisters it on the hub; revoke it on the
remote too if the hub shouldn't hold a working token.

### Where the phone keeps its token

A paired browser stores its device token in its own `localStorage` key,
`protoagent.deviceToken`, next to `protoagent.deviceId`. The operator token you type into
**Authentication required** stays in `protoagent.authToken`. Pairing a browser that already
holds the operator token no longer overwrites it. When both are present, the console sends
the device token. Entering a token in **Authentication required** replaces a device token
that stopped working.

Phones paired before this change kept their device token in `protoagent.authToken`. The
console moves it to the new key the next time it loads, without signing the phone out.

## The `auth` badge

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.

## Delegate to a paired remote

See [Pair a remote fleet member](/guides/pair-an-agent) for this operator procedure.
