# Windows desktop app

Install the desktop app on **Windows 11, 64-bit (x64)**, then follow
[Set up your first agent](/tutorials/first-agent) to connect a model and chat.
You do not need a source checkout, Python, or Node for basic chat.

<a id="_1-supported-system-verifying-the-asset"></a>

## Download

Use the official [download page](https://agent.protolabs.studio/download) and
choose the Windows installer. The release asset is named
`protoAgent-vX.Y.Z-x86_64-pc-windows-msvc-setup.exe`. There is no Windows ARM64 build.

<a id="_2-install-and-first-launch"></a>

## Install and first launch

1. Run the downloaded `setup.exe` and follow the installer.
2. The Windows build is unsigned. If SmartScreen shows **Windows protected your
   PC**, check that you downloaded it from the official source above before
   choosing **More info → Run anyway**. The prompt alone cannot establish whether
   a file is trustworthy.
3. Launch protoAgent from the Start Menu or desktop shortcut. On a fresh
   installation, the setup wizard opens. Continue with
   [Choose and name your agent](/tutorials/first-agent#_2-choose-and-name-your-agent).

If the window is blank or will not open, use [Recovery](#recovery).

<a id="_3-desktop-shell-frozen-server-and-the-console-—-how-the-pieces-relate"></a>

## App and browser access

The desktop app starts its own local server and stops it when you quit completely.
Closing the window can leave it running; use the tray icon's **Quit** action to stop it.

To use a browser with this same agent, open the server address shown in the
startup logs with `/app/` appended. Do not assume it is on port 7870: the app
chooses another port if that one is occupied. A browser on 7870 could show a
different instance. See [Recovery](#recovery) for log locations.

<a id="_4-where-your-data-lives"></a>

## Data and backups

Paste `%APPDATA%\studio.protolabs.protoagent\` into File Explorer's address bar
to find the desktop app's data. Work folders and explicitly overridden stores
can live elsewhere.

Installing a newer version or uninstalling the app leaves this data folder in
place. Reinstalling is not a backup and does not reset your configuration.
Use [Back up and restore your data](/guides/backup-and-restore) before recovery work;
it owns the steps for copying and restoring the app's data.

<a id="_5-updates-and-checking-your-version"></a>

## Updates

When an update is offered, review its release notes and choose **Update & Restart**.
See [Desktop controls](/guides/react-tauri-ui) for the shared app controls.
You can also install a newer official `setup.exe` over the existing installation.
Find your current version in **Settings → Overview**.

<a id="_6-managed-python-and-node-—-when-you-need-them"></a>

## Optional capabilities

Install extra runtimes only when the feature you want requires them:

- To create office documents or run Python code, follow [Enable document creation](/guides/python-runtime).
- For coding agents that require Node, follow [CLI coding agents](/guides/coding-agents).
- Before adding plugins, check [Desktop plugin compatibility](/guides/plugin-registry#dep-scope-which-interpreter-has-to-import-it).

<a id="_7-safe-recovery"></a>

## Recovery

Start with [App won't open](/guides/troubleshooting#app-won-t-open) for restart
and connection checks. For Windows-specific investigation:

- **Logs:** paste `%APPDATA%\studio.protolabs.protoagent\logs\` into File Explorer.
  Use the startup log's server address when testing the console in a browser.
- **Port conflict:** quit completely and relaunch. The app can choose a free port,
  but a second process using the same data folder still needs to be stopped;
  another port does not isolate data.
- **Broken installation:** back up the data, quit completely, uninstall through
  Windows Settings → Apps, and run the official installer again. Kept chats and
  settings should remain available; reinstalling will not fix an invalid model connection.
- **Fresh setup as a last resort:** follow the backup guide first, then rename
  the live app data folder while the app is stopped and relaunch. This creates
  empty state; it does not repair old chats or stores. Keep the original folder
  and use the [restore procedure](/guides/backup-and-restore#restore-a-backup-on-the-same-machine)
  to return to your saved data.

<a id="_8-where-to-go-next"></a>

## Next steps

Continue to [Use protoAgent](/guides/use-protoagent) for chat, files, and tools.
For a model or tool error, use [Troubleshooting](/guides/troubleshooting).
