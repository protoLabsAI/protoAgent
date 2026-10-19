# Access the app from your phone

Open the console on your phone or tablet to chat with the same agent and read its work.
The agent must stay running and reachable. You can add the console to your home
screen, but it has no offline mode.

## 1. Make the agent reachable {#_1-bind-to-the-network-set-a-token}

A phone can't reach an agent that only listens on `127.0.0.1`, which is the default. There
are two ways to fix that. Use the first one if you have Tailscale.

### Recommended: HTTPS via Tailscale {#https-via-tailscale}

[`tailscale serve`](https://tailscale.com/kb/1312/serve) gives the agent an HTTPS address on
your tailnet, such as `https://my-mac.tail1234.ts.net`. The agent stays on localhost:
Tailscale accepts the connection, handles HTTPS, and forwards it to the agent on this
machine. You get:

- **One address that doesn't change.** The name stays the same when the machine's IP
  changes, so a paired phone keeps working.
- **HTTPS with a real certificate.** The phone's browser treats the console as a secure
  site.
- **No bind change and no restart.**

The phone needs the Tailscale app, signed in to the same tailnet. Other devices on your
Wi-Fi can't use this address.

1. Install [Tailscale](https://tailscale.com/download) on the agent machine and on the
   phone. Sign both in to the same tailnet.
2. Turn on HTTPS for your tailnet. This is a one-time step for a tailnet admin. In the
   [Tailscale admin console](https://login.tailscale.com/admin/dns), open **DNS**, make
   sure **MagicDNS** is on, and under **HTTPS Certificates** select **Enable HTTPS**.
3. On the agent, open **Settings ▸ Devices ▸ Add a device** and select **Use HTTPS via
   Tailscale**. If the agent has no auth token, one is created and shown **once**. Save it.
4. The QR and the URL now point at `https://<machine>.<tailnet>.ts.net/app/`. Pair the
   phone as described in step 2 below.

To stop, select **Turn off Tailscale HTTPS** on the same card. That removes only this
agent's Tailscale handler. Any other `tailscale serve` setup on the machine is left alone.

**A token is required.** Tailscale forwards each request from this machine
(`127.0.0.1`). protoAgent never trusts a request because it comes from `127.0.0.1`, so a
request without the token is refused (`401`). An agent with **no** token, though, accepts
every request as the operator. With `tailscale serve` in front of it, every device on your
tailnet would get full control. So:

- The panel won't turn on HTTPS until a token is set. The server refuses too.
- If you set up `tailscale serve` by hand, or remove the token later, a tokenless agent
  refuses every request that arrives through a `*.ts.net` name (`403`). It also logs a
  warning. Set a token, or turn serve off. `PROTOAGENT_ALLOW_OPEN=1` turns this check off,
  along with the boot check.

**From a terminal or a headless box,** do the same thing by hand. Set a token first
(`auth.token` or `A2A_AUTH_TOKEN`), then run:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:7870
tailscale serve status            # check it
tailscale serve --https=443 off   # stop it
```

Use the agent's own port. If something else on the machine already uses HTTPS port 443,
pick `--https=8443`. The panel does this for you. On Linux, if `tailscale serve` says
access is denied, run `sudo tailscale set --operator=$USER` once.

| What you see | What it means |
| --- | --- |
| "HTTPS certificates aren't enabled on this tailnet" | Do step 2, then try again. The panel links to the admin page. |
| "Serve is not enabled on your tailnet" with a `login.tailscale.com` link | Open the link and approve Serve for this machine. |
| "Set an auth token first" | Expected on an agent with no token. Use the panel, which creates one, or set one yourself. |
| The QR still shows an IP address | Tailscale isn't connected on this machine, or serve isn't on. The panel shows **Use HTTPS via Tailscale** when it can be turned on. |
| The card says the address is also public through Funnel | `tailscale funnel` exposes the address to the internet. Turn it off unless you meant that. |

### Or: listen on a network address {#listen-on-a-network-address}

Without Tailscale, the agent has to listen on a network interface. Follow
[Make this agent reachable](/guides/pairing#before-you-pair-make-this-agent-reachable).
Pick the address in the panel. The agent listens on that address and on `127.0.0.1`, then
**Restart now** applies it. A Wi-Fi (LAN) address only works while the phone is on the same
Wi-Fi, and anything else on that network can reach it too.

## 2. Open it on the phone {#_2-reach-it-from-the-phone}

Pair the phone so it gets its own token. Pick whichever is easier:

- **Scan the QR.** On the agent, open **Settings ▸ Devices ▸ Add a device** and scan the
  QR with the phone's camera. See [Pair a phone (QR)](/guides/pairing#pair-a-phone-qr).
- **Type the code.** On the phone, open the URL shown beside the QR, tap
  **Have a pairing code?**, and type the ten-character code (`XXXXX-XXXXX`). See
  [Pair a phone by typing the code](/guides/pairing#pair-a-phone-by-typing-the-code).
- **No screen on the agent machine?** Run `protoagent pair --device` there. It prints the
  QR, the code and the URL in the terminal. See
  [Pair a phone from a headless box](/guides/pairing#pair-a-phone-from-a-headless-box).

Codes last five minutes and work once. Wrong codes never cancel a code; after too many,
the phone says **Too many attempts** and you wait a minute ([details](/guides/pairing#too-many-attempts)). The phone shows **Paired as &lt;name&gt;** when it worked, and appears in
Devices on the agent, where the row for the phone you're holding is marked **This device**.
To rename or revoke it, see [Manage devices](/guides/pairing#manage-devices).

Alternatively, open the console URL shown by the agent and enter the operator token when
asked. The desktop app can pick a port other than `7870`; the QR includes the right one.
On the phone, `localhost` means the phone itself, so it never works here.

The panel lists addresses best first. The QR uses the one you select.

| Address | Example | Requirement |
| --- | --- | --- |
| Tailnet HTTPS | `https://my-mac.tail1234.ts.net` | Tailscale app on the phone, signed in to the same tailnet. Works from any network. |
| Tailnet name | `http://my-mac.tail1234.ts.net:7870` | The same, over plain http. Offered when the agent listens on its tailnet address. |
| Tailnet IP | `http://100.64.0.5:7870` | The same. Breaks if the machine's tailnet IP changes. |
| Wi-Fi (LAN) | `http://192.168.1.20:7870` | Same Wi-Fi only. |

### Tailscale: reach it from another network {#tailscale-reach-it-from-anywhere}

Install [Tailscale](https://tailscale.com) on the agent machine **and** the phone, and
sign both in to the same tailnet. Tailscale access rules (ACLs) must allow the phone to
reach the machine. Then use [HTTPS via Tailscale](#https-via-tailscale), or select a
**Tailnet** address when pairing. The agent's token is still required. Tailscale only
provides the network path.

If the page does not load, check that the agent is running, that Tailscale is connected on
the phone, and that the phone can reach the address shown. After a bind change, check that
the agent restarted. On Wi-Fi, guest-network isolation or a firewall can block the
connection. If the page loads but rejects the token, pair again or enter a valid operator
token.

<!-- docs-media: phone-pairing -->

<!-- /docs-media -->

## 3. Add to Home Screen {#_3-add-to-home-screen}

Once the console opens, use the phone browser's **Add to Home Screen** action:
**Share** in iOS Safari, or the menu in Android Chrome. Use the resulting icon
to return to the console. If it asks for access again, pair it or enter the
operator token.

## On a tablet {#on-a-tablet}

A tablet pairs and connects the same way. What you see depends on its width:

- **Narrower than 768px** (some Android tablets held upright): the phone layout.
  Chat fills the screen; other views open over it from the menu.
- **Portrait, 768–1024px** (iPads held upright): the desktop layout with both
  rails, but chat uses the whole middle. The right panel (Work, notes, plugin
  views) starts closed. Tap its icon on the right rail to open it over chat,
  which dims behind it; tap the dimmed chat to close it. As on a phone, an
  artifact or code the agent shows does not open the panel by itself. A chip
  appears in chat; tap it to open the panel.
- **Landscape**: the desktop split, chat on the left and the right panel beside
  it. Drag the divider between them to resize. Artifacts and code the agent
  shows open in the right panel automatically, as on a desktop.

Buttons, tabs and the divider get finger-sized hit areas on a touch screen. An
iPad with a trackpad or mouse attached keeps the normal desktop split in either
orientation. Rotating from portrait to landscape reopens a right panel that
portrait closed.

## No offline mode

The home-screen shortcut still connects to the live agent. The console does
not cache an offline copy through a service worker. If the agent stops or the
network disconnects, reconnect before continuing the task.

## Use a public URL {#going-further-a-public-url}

The [public A2A proxy guide](/guides/exposing-protoagent) deliberately hides the
console and cannot be used for phone access as written. Prefer the private
connection above. If a public console hostname is required, use a separate
identity-protected hostname covering both the console and its backend routes,
with the application token still enabled.

## See also

- [Pair devices and agents](/guides/pairing) — individual tokens and revocation.
- [Run headless](/guides/headless) — server setup and authentication.
- [Troubleshooting](/guides/troubleshooting) — failed connections and tasks.
