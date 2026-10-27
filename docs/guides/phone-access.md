# Use protoAgent on your phone or tablet

Open the console on a phone or tablet to chat with the same agent and read its work. The phone
uses the agent running on your computer or server: same chats, same knowledge, same tools.
The agent has to keep running, and the phone has to be able to reach it.

The steps, in order:

1. [Choose how the phone reaches the agent](#choose-a-route): same Wi-Fi, Tailscale, or your
   own public URL.
2. [Pair the phone](#_2-reach-it-from-the-phone): scan a QR or type a code.
3. [Check what you see after pairing](#after-pairing).
4. [Add it to the home screen](#_3-add-to-home-screen).
5. [Use it on a tablet](#on-a-tablet).
6. [Move between networks](#move-between-networks).
7. [Manage access](#expiry-and-rotation), including [a lost phone](#lost-phone).
8. [Troubleshooting](#troubleshooting).

How pairing works underneath (codes, the claim limits, where the phone keeps its token) is in
[Manage paired devices](/guides/pairing). The security model is in
[Security & the trust model](/explanation/security-and-trust#device-tokens).

## 1. Choose how the phone reaches the agent {#choose-a-route}

<span id="_1-bind-to-the-network-set-a-token"></span>

An agent listens only on `127.0.0.1` by default, which a phone can't reach. On the phone,
`localhost` means the phone itself. Pick one of these routes:

| Route | Works from | The phone needs | Setup | Home-screen app |
| --- | --- | --- | --- | --- |
| [Tailscale with HTTPS](#https-via-tailscale) (recommended) | Any network | The Tailscale app, signed in to your tailnet | One switch in Devices, no restart | Yes (HTTPS) |
| [Your own public URL](#without-tailscale) | Any network | Nothing | A proxy or tunnel you run, such as Cloudflare Tunnel with Access | Yes (HTTPS) |
| [Same Wi-Fi](#listen-on-a-network-address) | The agent's Wi-Fi only | Nothing | Pick the address in Devices, then restart | A shortcut only (plain http) |

Every route needs an **auth token** on the agent. The panel creates one when the route needs
it and shows it **once**, so save it. You can offer more than one route; the phone keeps the
one it [pairs on](#move-between-networks).

### Tailscale with HTTPS {#https-via-tailscale}

<span id="tailscale-reach-it-from-anywhere"></span>

[`tailscale serve`](https://tailscale.com/kb/1312/serve) gives the agent an HTTPS address on
your tailnet, such as `https://my-mac.tail1234.ts.net`. The agent stays on localhost:
Tailscale accepts the connection, handles HTTPS, and forwards it to the agent on this machine.
You get one address that doesn't change when the machine's IP does, a real certificate, and no
bind change or restart. Only devices on your tailnet can use the address, and only if your
Tailscale access rules (ACLs) let them reach this machine.

1. Install [Tailscale](https://tailscale.com/download) on the agent machine and on the phone.
   Sign both in to the same tailnet.
2. Turn on HTTPS for your tailnet. This is a one-time step for a tailnet admin. In the
   [Tailscale admin console](https://login.tailscale.com/admin/dns), open **DNS**, make sure
   **MagicDNS** is on, and under **HTTPS Certificates** select **Enable HTTPS**.
3. On the agent, open **Settings ▸ Devices ▸ Add a device** and select **Use HTTPS via
   Tailscale**. If the agent has no auth token, one is created and shown once.
4. The QR and the URL now point at `https://<machine>.<tailnet>.ts.net/app/`. Go on to
   [pair the phone](#_2-reach-it-from-the-phone).

To stop, select **Turn off Tailscale HTTPS** on the same card. That removes only this agent's
handler; any other `tailscale serve` setup on the machine is left alone.

**Why a token is required.** Tailscale forwards each request from `127.0.0.1`, and protoAgent
never trusts a request for coming from `127.0.0.1`, so a request without a token gets `401`.
An agent with **no** token, though, accepts every request as you, so every device on your
tailnet would get full control. The panel and the server both refuse to turn HTTPS on until a
token is set. If you set up `tailscale serve` by hand, or remove the token later, a tokenless
agent refuses every request that arrives through a `*.ts.net` name (`403`) and logs a warning.
`PROTOAGENT_ALLOW_OPEN=1` turns that check off, along with the boot check; don't use it here.

**From a terminal or a headless box**, set a token first (`auth.token` or `A2A_AUTH_TOKEN`),
then:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:7870
tailscale serve status            # check it
tailscale serve --https=443 off   # stop it
```

Use the agent's own port; the desktop app may not use `7870`. If something else on the machine
already uses HTTPS port 443, use `--https=8443` (the panel does this for you). On Linux, if
`tailscale serve` says access is denied, run `sudo tailscale set --operator=$USER` once.

**Then set the agent's public URL** to the same HTTPS address: **Settings ▸ Box ▸ Network ▸
Public URL** (`network.public_url`), then restart. Without it, links in answers (images, files,
links into the console) point at `127.0.0.1`, which opens nothing on a phone. Fleet members pick
up their own `…/agents/<id>` address from it.

Without `tailscale serve`, a phone on your tailnet can still use the agent's plain-http
**Tailnet** address. That needs the agent to listen on its tailnet address, set up the same way
as [Same Wi-Fi](#listen-on-a-network-address), and it can't be installed as an app.

### Your own public URL {#without-tailscale}

<span id="going-further-a-public-url"></span>

A phone without Tailscale can still pair and work from any network, through an HTTPS address
**you** run in front of the agent: Cloudflare Tunnel, nginx or Caddy. protoAgent doesn't host
one for you.

**Pick the posture first.** With Cloudflare Access (or another identity-aware proxy) in front,
nobody on the internet reaches the console until they sign in to Access. Without it, anyone can
load the sign-in and try pairing codes; only the agent's token, the
[claim throttle](/explanation/security-and-trust#the-pairing-claim-endpoint) and the phone's
[Limited scope](#after-pairing) protect it. Use Access unless you have a reason not to.

1. **Set an auth token** on the agent if it doesn't have one. The agent refuses to start with a
   public URL and no token, and `PROTOAGENT_ALLOW_OPEN=1` doesn't change that.
2. **Give the console its own hostname**, such as `console.example.com`, and point your proxy
   at the agent for the **whole** hostname. Don't reuse a hostname that serves A2A to the
   public: that one keeps `/app` and `/api` blocked. The
   [console hostname recipe](/guides/exposing-protoagent#console-hostname) has a Cloudflare
   Tunnel and Access setup, plus nginx and Caddy blocks.
3. **Declare it.** Set `network.public_console_url` to the origin, with no path:

   ```yaml
   network:
     public_console_url: https://console.example.com
   ```

   Or set `PROTOAGENT_PUBLIC_CONSOLE_URL`, or edit **Settings ▸ Box ▸ Network ▸ Public console
   URL**. It must start with `https://`. It applies on save.
4. **Trust the proxy**, if it reaches the agent from another address (a container on a Docker
   network, another machine). List that address in
   [`network.trusted_proxies`](/reference/configuration#network) and restart. A proxy on the
   same machine needs nothing.
5. **Pair the phone** with the **Phone without Tailscale** address. The panel picks it for you
   when there's no tailnet HTTPS address. If Access is on, the phone signs in to Access first,
   then pairs.

While the URL is set, Devices warns that anyone on the internet can reach the console's
sign-in. To make the console private again, clear the setting.

### Same Wi-Fi {#listen-on-a-network-address}

Without Tailscale or a proxy, the agent has to listen on a network address. Open
**Settings ▸ Devices ▸ Add a device**, pick the **Wi-Fi** address in the notice, and select
**Restart now**. The agent then listens on that address and on `127.0.0.1`, so the desktop app
keeps working. What picking an address changes, how to undo it, and what to do when
**Restart now** isn't offered are in
[Make this agent reachable](/guides/pairing#before-you-pair-make-this-agent-reachable).

A Wi-Fi address works only while the phone is on that Wi-Fi, and anything else on the network
can reach it too. In a container, publish the port in your compose file instead; the bind
inside the container doesn't decide what a phone can reach.

### Keep the computer awake {#keep-awake}

Whatever route you pick, the agent only answers while the computer is awake. A Mac that goes
to sleep suspends protoAgent along with Tailscale, so a phone that was reaching the agent fine
suddenly can't — the address stops answering until someone wakes the machine.

To keep a computer that serves other devices from sleeping, turn on **Settings ▸ Runtime ▸
Stay awake while serving** (`runtime.stay_awake`), then restart. While it's on, the server holds
the computer awake for as long as it runs.

- **On a Mac it applies only on AC power.** On battery the computer sleeps as usual, so plug in
  a Mac you want reachable. Closing a laptop's lid can still sleep it even on AC power.
- **No-code alternative on macOS:** open **System Settings ▸ Battery** (or **Energy** on an
  Intel Mac) and turn on **Prevent automatic sleeping when the display is off**. This keeps the
  machine awake without touching protoAgent's config.
- **To disable it without changing the setting,** start the server with the environment variable
  `PROTOAGENT_KEEP_AWAKE=0`.
- **Not supported on Windows** — the toggle has no effect there; keep the PC awake through its
  own power settings.

## 2. Pair the phone {#_2-reach-it-from-the-phone}

Pairing gives the phone its own token, so you never type the agent's auth token on it. Pick
whichever is easiest:

- **Scan the QR.** On the agent, open **Settings ▸ Devices ▸ Add a device** and scan the QR
  with the phone's camera. The console claims the code and removes it from the address bar.
- **Type the code.** On the phone, open the URL shown beside the QR, tap
  **Have a pairing code?**, type the ten-character code (`XXXXX-XXXXX`), and tap
  **Pair this device**. Case and the dash do not matter; O, I and L are read as
  0, 1 and 1.
- **No screen on the agent machine?** Run `protoagent pair --device` there. It prints the QR,
  the code and the URLs in the terminal. See
  [Pair a phone from a headless box](/guides/pairing#pair-a-phone-from-a-headless-box).

A code lasts five minutes and works once. The QR and the typed code are the same code.

If the agent offers more than one address, the panel lists them best first and the QR uses the
one you select. The one you pair on becomes the phone's home address, so pick the one that fits
the phone ([Move between networks](#move-between-networks)):

| Address | Example | Requirement |
| --- | --- | --- |
| Tailnet HTTPS | `https://my-mac.tail1234.ts.net` | Tailscale app on the phone, signed in to the same tailnet. Works from any network. |
| Phone without Tailscale | `https://console.example.com` | Your own [public URL](#without-tailscale). Works from any network. Listed only when you've set one. |
| Tailnet name | `http://my-mac.tail1234.ts.net:7870` | The same as Tailnet HTTPS, over plain http. Offered when the agent listens on its tailnet address. |
| Tailnet IP | `http://100.64.0.5:7870` | The same. Breaks if the machine's tailnet IP changes. |
| Wi-Fi (LAN) | `http://192.168.1.20:7870` | Same Wi-Fi only. |

You can also open the console URL on the phone and enter the agent's auth token when asked.
That browser then isn't a paired device: it has no row in Devices, and you can't remove it
without changing the token. Pairing is the better choice.

<!-- docs-media: phone-pairing -->
<figure class="docs-media"><img src="/media/phone-pairing/v1.png" alt="The desktop lists an active limited-scope iPhone device, beside an emulated phone console confirming successful pairing." loading="lazy" style="max-width:100%;height:auto"><figcaption>Confirm the device on both sides. This actual pairing uses an emulated phone viewport; a physical phone needs its own reachable agent URL.</figcaption></figure>
<!-- /docs-media -->

## 3. After pairing: what you see {#after-pairing}

- **On the phone:** **Paired as &lt;name&gt;**. The name is a guess from the browser, such as
  `iPhone` or `Android phone`.
- **On the agent:** a **Device paired** notice, and a new row in **Settings ▸ Devices**.

Open **Settings ▸ Devices** on the phone to see its row:

- **This device** marks the row for the phone you're holding. Rename it there to replace the
  guessed name ([Rename a device](/guides/pairing#rename-a-device)).
- **Limited** is the phone's access. Every phone or tablet you pair is Limited.
- **expires in 30 days if unused** says how long the pairing lasts without use
  ([Manage access](#expiry-and-rotation)).
- **active** means it made a request in the last few minutes.

A Limited phone can chat and use the agent's tools, but cannot change credentials,
install integrations, or pair other devices. It is not a sandbox for chat tools.
Use [device access controls](/guides/pairing#full-access-and-limited) to inspect
or change access from your desktop.

## 4. Add it to the home screen {#_3-add-to-home-screen}

Once the console opens, use the browser's **Add to Home Screen** action: **Share** in iOS
Safari, or the menu in Android Chrome. Use the icon to return to the console.

- On an **HTTPS** address (Tailnet HTTPS or your public URL), the console installs as an app
  that opens full screen, without the browser's address bar.
- On a plain **http** address (Wi-Fi, a tailnet name or IP), the browser treats the console as
  an insecure site. Android Chrome only adds a shortcut that opens in a browser tab. To get the
  app, pair on an HTTPS address, or [move the phone](#move-this-phone) to one.

The icon belongs to the address you added it from. After moving the phone, add the new address
and remove the old icon.

### No offline mode {#no-offline-mode}

The home-screen icon still connects to the live agent. The console keeps no offline copy, so
when the agent stops or the network drops, reconnect before you go on.

## 5. On a tablet {#on-a-tablet}

A tablet pairs and connects the same way. What you see depends on its width:

- **Narrower than 768px** (some Android tablets held upright): the phone layout. Chat fills
  the screen; other views open over it from the menu.
- **Portrait, 768–1024px** (iPads held upright): the desktop layout with both rails, but chat
  uses the whole middle. The right panel (Work, notes, plugin views) starts closed. Tap its
  icon on the right rail to open it over chat, which dims behind it; tap the dimmed chat to
  close it. An artifact or code the agent shows does not open the panel by itself. A chip
  appears in chat; tap it to open the panel.
- **Landscape**: the desktop split, chat on the left and the right panel beside it. Drag the
  divider between them to resize. Artifacts and code the agent shows open in the right panel
  automatically, as on a desktop.

Buttons, tabs and the divider get finger-sized hit areas on a touch screen. An iPad with a
trackpad or mouse attached keeps the normal desktop split in either orientation. Rotating from
portrait to landscape reopens a right panel that portrait closed.

## 6. Move between networks {#move-between-networks}

A phone has one **home address**: the one it was paired on. Its token, its chat history and its
home-screen icon all belong to that address, so the phone always opens the console there. Pick a
home that works wherever you take the phone:

| The phone has | Make its home | Works |
| --- | --- | --- |
| Tailscale | **Tailnet HTTPS** | On any network |
| No Tailscale, and you've set a [public URL](#without-tailscale) | **Phone without Tailscale** | On any network, home Wi-Fi included |
| Neither | **Wi-Fi** | On the agent's Wi-Fi only |

With a home that works on any network, the phone never needs moving.

### Move this phone to another address {#move-this-phone}

A phone paired on the Wi-Fi address works only at home. To give it a home that works away too,
move it while it can still reach the agent:

1. On the phone, open **Settings ▸ Devices**.
2. On the row marked **This device**, tap **Move…**. The list shows the agent's other
   addresses, each with what the phone needs for it, such as "needs the Tailscale app on this
   phone".
3. Pick one, check the confirmation, and tap **Move**.

The phone opens the console at the new address and shows **Moved &lt;name&gt; here**. It stays
the same device in Devices, with the same name and access. Its token is replaced, and the old
address stops working for it: opening the old address afterwards says
**This phone moved to &lt;address&gt;**. If the move fails partway, for example because the new
address doesn't load, the old address keeps working.

**Move…** appears only on the phone itself, and only when the agent has another address to
offer. The list follows the agent's current setup: turn on Tailscale HTTPS or set a public URL,
and the phone sees the new address the next time it opens the console.

### When the home address stops answering {#when-home-stops-answering}

If the phone loses its home address while the console is open, for example because you left the
Wi-Fi it was paired on, a **can't reach this agent here** banner lists the agent's other
addresses as links, each with what it needs. An address that answered a quick check is marked
**answering**. The check is only a hint: a phone browser can't always check an address.

Opening one of those links means pairing the phone again at that address, because the token
stays with the home address. To avoid that, [move the phone](#move-this-phone) while you're
still at home. The banner clears when the home address answers again.

<span id="expiry-and-rotation"></span>
<span id="remove-or-limit"></span>

## 7. Manage access

A Limited phone expires after 30 days without use by default. If it asks you to
sign in again, check its row in **Settings → Devices** on the desktop. Follow
[device recovery](/guides/pairing#expired) for expiry and
[token rotation](/guides/pairing#weekly-token-rotation) for copied-token detection.

Use [Manage paired devices](/guides/pairing#manage-devices) for renaming,
[access levels](/guides/pairing#full-access-and-limited), and
[removing a device](/guides/pairing#revoke). The desktop can manage every row;
a Limited phone can rename or remove only itself.

### If a phone is lost {#lost-phone}

1. On the desktop app (or another full-access device), open **Settings ▸ Devices** and remove
   the phone's row. This is the step that matters: its token is dead from the next request.
2. If you ever typed the agent's auth token into that phone (instead of pairing it), change the
   token under **Settings ▸ Operator & access**. Removing a device doesn't touch the auth token.
3. If the phone had **Full access**, check the Devices list for rows you don't recognise and
   remove them. A full-access phone can pair new devices.
4. Remove the phone from your tailnet in the Tailscale admin console, or end its sessions in
   Cloudflare Access, so it loses the network path too.

Don't wait for expiry: 30 days unused is a backstop, not a lock.

## 8. Troubleshooting {#troubleshooting}

| What you see | What it means | What to do |
| --- | --- | --- |
| **Too many attempts — try again in N s** (`429`) | Too many wrong codes arrived, from this phone or from anywhere. Your code wasn't checked. | Wait, then try the same code again if it hasn't expired. Behind a proxy on another address, set [`network.trusted_proxies`](/reference/configuration#network). See [Too many attempts](/guides/pairing#too-many-attempts). |
| **That code didn't work** | The code is mistyped, expired or already used. | See [If the code doesn't work](/guides/pairing#if-the-code-doesnt-work). |
| **Couldn't reach this agent**, or the page doesn't load | A network problem, not the code. | Check the agent is running, the phone is on the same Wi-Fi or Tailscale is connected on it, and the address works. Guest-network isolation or a firewall can block a Wi-Fi address. |
| The phone can't reach the agent after the computer has been idle | The computer slept, which suspends protoAgent and Tailscale, so the address stopped answering. | Wake the computer, then [keep it awake while serving](#keep-awake). |
| `protoagent pair --device` says the agent runs with `--ui none` (`no_console`) | There is no console for the phone to open, so no code is made. | Restart the agent with `--ui console`, the default. |
| **Restart protoAgent to finish** in Devices | A new bind address is saved but applies only at startup. | Select **Restart now**, or stop and start the agent the way you launched it. |
| "This agent was started with `--host` …, which overrides the bind setting" | The command line pins the address, and a restart keeps it. | Start the agent again without `--host`, or with the address you want. |
| "This agent runs in a container" | Phones reach the ports the container publishes. | Publish the port in your compose file. |
| "pairing refused: this request reached the public console address over plain http …" (`public_not_https`) | The proxy served http, or the agent doesn't trust the proxy's `X-Forwarded-Proto`. | Serve the public URL over https, and list the proxy's address in [`network.trusted_proxies`](/reference/configuration#network), then restart. |
| The agent won't start: "refusing to start: network.public_console_url is set … but this agent has NO auth token" | A public console with no token would give anyone full control. `PROTOAGENT_ALLOW_OPEN` doesn't override it. | Set `auth.token` or `A2A_AUTH_TOKEN`, or remove the public URL. |
| "HTTPS certificates aren't enabled on this tailnet" | Tailscale HTTPS is off for your tailnet. | Turn it on in the Tailscale admin console (**DNS ▸ HTTPS Certificates**), then try again. |
| "Serve is not enabled on your tailnet", with a `login.tailscale.com` link | Tailscale wants Serve approved for this machine. | Open the link and approve it. |
| "Set an auth token first" | Tailscale HTTPS needs a token. | Use the panel, which creates one, or set one yourself. |
| The QR still shows an IP address | Tailscale isn't connected on this machine, or serve isn't on. | Connect Tailscale, then select **Use HTTPS via Tailscale**. |
| The card says the address is also public through Funnel | `tailscale funnel` exposes the address to the internet. | Turn Funnel off unless you meant that. |
| **This device can't change pairing or credentials — use a full-access device.** | The phone is [Limited](#after-pairing). | Do it from the desktop app, or Promote the phone there. |
| **This phone moved to &lt;address&gt;** | You [moved the phone](#move-this-phone). | Open the console at the new address. |
| The phone asks for a token again | It was removed, it [expired](#expiry-and-rotation), or a copied token got it removed. | Remove its row if it's still listed, then pair again. |

More: [Troubleshooting](/guides/troubleshooting) for failed connections and tasks.

## See also

- [Manage paired devices](/guides/pairing): pairing mechanics, the Devices list, revocation.
- [Expose to the world](/guides/exposing-protoagent): public A2A and the console hostname.
- [Security & the trust model](/explanation/security-and-trust#device-tokens): device scope
  and token lifetime.
- [Run headless](/guides/headless): a server with no screen.
- [Operator API](/reference/operator-api#device-scope-refusals) and
  [configuration](/reference/configuration#devices) reference.
