# Access the app from your phone

Open the console on your phone or tablet to chat with the same agent and read its work.
The agent must stay running and reachable. You can add the console to your home
screen, but it has no offline mode.

## 1. Make the agent reachable {#_1-bind-to-the-network-set-a-token}

Follow [Make this agent reachable](/guides/pairing#before-you-pair-make-this-agent-reachable).
It covers desktop and terminal-launched servers, saving the operator token, and
restarting after a bind change. Choosing an address enables listening on all
interfaces; use firewall or tailnet rules to limit reachability.

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

Codes last five minutes and work once. Five wrong codes in a row cancel every pending
code; make a new one. The phone shows **Paired as &lt;name&gt;** when it worked, and appears in
Devices on the agent, where the row for the phone you're holding is marked **This device**.
To rename or revoke it, see [Manage devices](/guides/pairing#manage-devices).

Alternatively, open `http://<host>:<port>/app/` and enter the operator token
when asked. Use an address and port shown by the agent. Desktop can choose a
port other than `7870`; the QR contains the selected one. A phone's `localhost`
points to the phone itself.

| Connection | Host address | Requirement |
| --- | --- | --- |
| Same Wi-Fi | The agent machine's LAN address, such as `192.168.1.20` | Both devices can reach each other on that network |
| Tailscale | The agent machine's tailnet address or MagicDNS name | Tailscale is connected on both devices and its access rules allow the connection |

### Tailscale: reach it from another network {#tailscale-reach-it-from-anywhere}

Install [Tailscale](https://tailscale.com) on the agent machine and phone, and
connect both to your tailnet. Select the agent's **Tailnet** address when pairing.
The app's token is still required; Tailscale supplies the network connection.

If the page does not load, check that the agent is running, its network bind
change took effect after restart, and the phone can reach the displayed address.
On Wi-Fi, guest-network isolation or a firewall can block the connection. If it
loads but rejects a token, pair again or supply a valid operator token.

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
