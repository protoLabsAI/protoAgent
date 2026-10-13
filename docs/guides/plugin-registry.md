# Install and manage plugins

Plugins add tools, integrations, and views. You need a running agent and a plugin
source you trust: installing from the app enables and runs its code with the
server's privileges.

## Install one

1. Open **Settings → Plugins → Discover**.
2. Find the plugin and review its description and source. Choose **Install**.
   For a repository URL instead, open **Installed → Install from URL**, enter
   the URL and optional release tag, then choose **Install**.
3. Read any source-trust confirmation. If the plugin needs Python packages,
   review the listed packages and their installation location before continuing.
   **Not now** keeps the plugin installed without installing those packages.
4. In **Installed**, confirm the plugin is **Loaded**. Use **Configure** if it
   needs credentials or other settings.
5. Try one task using its tool or open its view. A loaded plugin can still need
   an external service or login before that task succeeds.

If installation fails, keep the error shown by the dialog. Check the repository
URL, access to it, and the required app version before retrying. Do not reinstall
repeatedly to fix missing packages; use [Install dependencies](#installing-a-plugin-s-deps-from-the-console).

## Enable or disable

In **Settings → Plugins → Installed**, use the plugin's enable toggle. Enabling
adds its tools and views; disabling removes them and stops its background work.
Settings normally apply immediately. Restart only if the app requests it.

Use the **Attention** filter to find load errors, unfinished setup, missing
packages, and available updates. Search by plugin name or tool name to locate
which plugin provides a capability.

## Installing a plugin's deps from the console

If a plugin reports missing packages, choose **Install deps** on its row, or
**Install dependencies** in its warning banner. Wait for completion, then retry
the task. On failure, read the error and use **Retry** after fixing its cause.

On desktop, some plugins need the [managed Python runtime](/guides/python-runtime).
Install it from **Settings → Tools** if the dependency action says it is missing.
A required package imported by the app's own frozen process cannot be supplied
by that runtime; see [Desktop compatibility](#dep-scope-which-interpreter-has-to-import-it).

Optional packages enable additional features and may be absent without preventing
the plugin from loading. Check the task's error for the package it actually needs.

## Keep one up to date

Open **Installed** and choose **Update** when a plugin has an update available.
The app fetches the update and reloads an enabled plugin. Check its status and
try one task afterwards. If the app asks for a restart, quit and reopen it.

| Status | Meaning |
| --- | --- |
| **up to date** | The installed commit matches its recorded ref |
| **update available** | The remote ref has moved; Update can fetch it |
| **pinned** | It targets an exact commit and does not follow newer commits |
| **check failed** | The remote update check failed; inspect the reason |

A plugin update can add dependencies. If it stops working, check **Attention**
and install any newly required packages. To return to an earlier version, keep
a [data backup](/guides/backup-and-restore) before updating or record the prior
release ref and reinstall it explicitly.

## Remove a plugin

Disable a plugin first if you only want to stop using it. For a downloaded plugin,
choose **Uninstall** and read the confirmation before accepting.

Uninstall removes its code and installation record. Config and credentials are
kept by default, so reinstalling can reuse them. Separately installed Python
packages are kept too. Bundled plugins remain part of the app; disable them
instead of trying to delete their files.

For a bundle, the uninstall confirmation names the plugins it will remove.
Members shared with another bundle are retained.

## Recover a missing plugin

If the app reports plugins recorded in `plugins.lock` but missing on disk, choose
**Sync plugins**. This downloads the pinned copies again. Previously enabled
plugins can run as soon as the sync finishes; use it only for sources you trust.

Sync restores plugin code, not your chats, credentials, or plugin data. Use a
[data backup](/guides/backup-and-restore) to recover those.

## Dep scope: which interpreter has to import it

A desktop plugin can be refused because it imports a required package that the
app does not bundle. Installing that package into the managed document runtime
does not fix the app's own imports. Use a compatible plugin or app release, or
ask the author to address the missing host dependency. Authors should follow
[Dependency scopes](/guides/publish-a-plugin#dep-scope-which-interpreter-has-to-import-it).

## Use the CLI

An installed Python package also provides:

```bash
protoagent plugin install https://github.com/owner/your-plugin --ref v1.0
protoagent plugin list
protoagent plugin install-deps your-plugin
protoagent plugin sync
protoagent plugin uninstall your-plugin
```

CLI installation fetches code without enabling it. Enable it in **Settings →
Plugins → Installed** afterwards. In a source checkout, replace `protoagent`
with `uv run python -m server`. Make sure the CLI targets the same instance as
the app; [Configuration](/reference/configuration#file-locations) explains the roots.

## Safety

Review the source before installing. The manifest describes intended capabilities;
it does not sandbox the plugin. Source confirmations and dependency dialogs are
separate decisions: accepting one does not establish that the other is safe.

For enforced source allowlists and dependency declarations, see
[Publishing safety](/guides/publish-a-plugin#safety) and
[Security and trust](/explanation/security-and-trust).

## For plugin authors

The authoring sections moved to [Publish a plugin](/guides/publish-a-plugin).
These links preserve the earlier section URLs:

### When a plugin moves into core (`supersedes`)

See [Moving a plugin into core](/guides/publish-a-plugin#when-a-plugin-moves-into-core-supersedes).

### Keep a bundle fresh (the pin lifecycle)

See [Bundle pins](/guides/publish-a-plugin#keep-a-bundle-fresh-the-pin-lifecycle).

### Publish one

See [Publish one](/guides/publish-a-plugin#publish-one).

### Platform-specific deps (environment markers)

See [Platform-specific dependencies](/guides/publish-a-plugin#platform-specific-deps-environment-markers).

### Get listed in the directory

See [Directory listing](/guides/publish-a-plugin#get-listed-in-the-directory).
