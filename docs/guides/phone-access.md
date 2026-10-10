# Access the app from your phone

Open the console on your phone to chat with the same agent and read its work.
The agent must stay running and reachable. You can add the console to your home
screen, but it has no offline mode.

## 1. Make the agent reachable {#_1-bind-to-the-network-set-a-token}

For the desktop app:

1. Open **Settings → Devices → Add a device**.
2. If the agent is bound to localhost, choose an address from the reachability
   notice: **Tailnet** for your Tailscale devices, or **Wi-Fi** for the local network.
3. If the app creates a token, **Copy** it and save it. Other browsers, the CLI,
   or the desktop app after restart may need it; it is only shown once.
4. When **Restart protoAgent to finish** appears, quit from the tray or menu bar
   and reopen the app. Closing the window alone can leave the server running.
5. Open **Settings → Devices → Add a device** again.

Choosing an address makes the server listen on all network interfaces; it does
not limit listening to that one address. Requests require a token. Use your
firewall or tailnet access rules to limit which machines can reach it. See
[Pair devices and agents](/guides/pairing#before-you-pair-make-this-agent-reachable)
for reversing this change.

::: details Start a source or package server for phone access
Generate a token and keep its displayed value before starting the server:

```bash
PROTOAGENT_PHONE_TOKEN=$(openssl rand -hex 24)
printf '%s\n' "$PROTOAGENT_PHONE_TOKEN"
A2A_AUTH_TOKEN="$PROTOAGENT_PHONE_TOKEN" protoagent serve --host 0.0.0.0 --port 7870
```

In a source checkout, replace `protoagent serve` with
`uv run python -m server`. You must have built the console first; see
[Build and test the console](/guides/build-console).
:::

## 2. Open it on the phone {#_2-reach-it-from-the-phone}

Scan the QR from **Add a device**. The phone receives its own token, and its
entry appears in the agent's Devices list. If the code expired or the agent
restarted, choose **New code** and scan again.

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

## 3. Add to Home Screen {#_3-add-to-home-screen}

Once the console opens, use the phone browser's **Add to Home Screen** action:
**Share** in iOS Safari, or the menu in Android Chrome. Use the resulting icon
to return to the console. If it asks for access again, pair it or enter the
operator token.

## No offline mode

The home-screen shortcut still connects to the live agent. The console does
not cache an offline copy through a service worker. If the agent stops or the
network disconnects, reconnect before continuing the task.

## Use a public URL {#going-further-a-public-url}

For access without joining a LAN or tailnet, follow
[Expose to the world](/guides/exposing-protoagent). Keep authentication enabled
and set the public address advertised to other agents. Public exposure also
makes the operator routes reachable, including configuration and plugin installation.

## See also

- [Pair devices and agents](/guides/pairing) — individual tokens and revocation.
- [Run headless](/guides/headless) — server setup and authentication.
- [Troubleshooting](/guides/troubleshooting) — failed connections and tasks.
