# Windows desktop app

The packaged Windows build (`setup.exe`) is the no-checkout way to run protoAgent: one
installer, a bundled server, a window. This page covers **install → first launch → data
locations → updates → recovery** for that specific build. It routes to the deeper pages
rather than repeating them:

- What the console *is* and how it's laid out — [Operator console (React/Tauri)](/guides/react-tauri-ui).
- Running from a source checkout instead (Unix-flavored, `python -m server`) — [Spin up your first agent](/tutorials/first-agent).
- The one-click interpreter for `execute_code` / document skills — [Managed Python runtime (desktop)](/guides/python-runtime).

## 1. Supported system + verifying the asset

The desktop build ships for **Windows 11, 64-bit (x64)** only — there's no ARM64 Windows
build today. The installer is published as a GitHub Release asset named
`protoAgent-vX.Y.Z-x86_64-pc-windows-msvc-setup.exe`; the [download page](https://agent.protolabs.studio/download)
always links the newest release that actually shipped one, so download from there (or
from the repo's [Releases page](https://github.com/protoLabsAI/protoAgent/releases))
rather than a link found anywhere else. Check the version in the filename against the
release you meant to get — there's no separate checksum file today (see the note on
signing right below, which is the bigger reason to be deliberate about the source).

## 2. Install and first launch

Run the downloaded `setup.exe` and follow the installer.

> **The Windows build is unsigned.** There's no Windows code-signing certificate yet
> ([`.github/workflows/desktop-build.yml`](https://github.com/protoLabsAI/protoAgent/blob/main/.github/workflows/desktop-build.yml)
> builds it explicitly unsigned), so **expect a SmartScreen "Windows protected your PC"
> prompt** on both the installer and the first run of the app. That's expected for this
> build, not a sign of tampering — but it also means Windows itself can't vouch for the
> binary the way it can for a signed one, so only install it from the official sources
> above. Click **More info → Run anyway** to proceed. This is different from the macOS
> build, which *is* signed and notarized and opens with no Gatekeeper prompt.

First launch opens the same **setup wizard** the console always shows before
`config/.setup-complete` exists — connect a model endpoint/key (or a ChatGPT/Claude
subscription), name the agent, review the starter tools, and launch. That flow is
platform-independent and already covered step-by-step in
[Spin up your first agent § 3](/tutorials/first-agent#_3-open-the-setup-wizard) — read
that for the field-by-field walkthrough; nothing about it is Windows-specific **except
one wizard field**:

> **"Autostart on boot" reports unsupported on Windows.** The source-checkout tutorial's
> autostart toggle installs a **macOS-only** LaunchAgent. On Windows the same toggle
> (Settings ▸ Runtime) is present but honestly reports "not yet supported on this
> platform" instead of doing anything — Task Scheduler autostart isn't implemented yet.
> There is currently no "start protoAgent at login" option on Windows; launch it from the
> Start Menu / desktop shortcut the installer creates.

## 3. Desktop shell, frozen server, and the console — how the pieces relate

Three things are running, and it helps to keep them separate when something goes wrong:

1. **The desktop shell** — a Tauri window (`protoagent_desktop.exe`) that owns the tray
   icon, global hotkeys, the update check, and the window chrome.
2. **The frozen server** (`protoagent-server.exe`) — a PyInstaller-frozen build of the
   *same* FastAPI + LangGraph server a source checkout runs, spawned by the shell as a
   child process ("sidecar") on first launch and torn down when the shell exits. It binds
   `127.0.0.1:7870` (or the next free port if something already holds 7870 — see
   [Recovery](#_7-safe-recovery) below).
3. **The console** — the same React app described in
   [Operator console (React/Tauri)](/guides/react-tauri-ui): normally the desktop window
   just displays it, but it's also reachable at `http://127.0.0.1:7870/app/` in an
   ordinary browser tab while the app is running — useful when the app window itself is
   stuck (see Recovery).

This is **one local agent**, not a fleet and not a remote deployment. A
[headless server](/guides/headless) (no UI, API + A2A only) or a **separately deployed**
agent — Docker/GHCR ([Deploy via GHCR](/guides/deploy)), a Linux box, another machine
entirely — is a different, independent install with its own data; the desktop app has no
special relationship to one unless you explicitly add it as a
[delegate](/guides/delegates) or fleet member.

## 4. Where your data lives

The desktop build points **all** writable state — config, plugin installs, and every
per-instance store (checkpoints, knowledge, memory, scheduler, tasks, telemetry, …) — at
one per-user folder:

```
%APPDATA%\studio.protolabs.protoagent\
```

(`%APPDATA%` is normally `C:\Users\<you>\AppData\Roaming`.) Logs live one level inside
that same folder, at `%APPDATA%\studio.protolabs.protoagent\logs\` — unlike the macOS
build, where logs go to a separate `~/Library/Logs/...` tree.

This is the box root *and* the instance root for this install (see
[ADR 0065](../adr/0065-two-tier-instance-paths.md) for the general model) — there's
nothing else on disk to know about. It's also exactly what the installer's uninstall
hooks are careful **never** to touch: uninstalling and reinstalling (or installing a
newer `setup.exe` over an older one) leaves this folder alone, so your config, secrets,
knowledge, and chat history all survive. That's the supported "keep data" upgrade path —
see [Recovery](#_7-safe-recovery) below for when you'd actually reach for it.

## 5. Updates and checking your version

The shell checks the [GitHub Releases](https://github.com/protoLabsAI/protoAgent/releases)
updater manifest once at launch (in parallel with the server booting) and again on a
6-hour cycle while running; a newer build surfaces as an **in-app update prompt** with the
changelog, and installing it relaunches the app. You don't need to re-download `setup.exe`
for routine updates — that's only for a first install, or if you want to jump straight to
a specific release.

**To confirm what you're running right now**, open the top-right **Menu** (hamburger)
button in the console — the footer shows `v<version>`.

## 6. Managed Python and Node — when you need them

The frozen server has no system Python behind it (`sys.executable` *is* the app, not an
interpreter), so two capabilities need an explicit, one-click provision step the first
time you use them — neither is required to chat with your agent:

- **`execute_code` and the document skills** (docx / xlsx / pptx / PDF) need the
  **managed Python runtime** — see [Managed Python runtime (desktop)](/guides/python-runtime)
  for the install card location and what it downloads.
- **`npx`-based ACP coding agents and most catalog MCP servers** need Node on `PATH`,
  which the desktop bundle doesn't ship — see
  [CLI coding agents over ACP § no Node installed at all](/guides/coding-agents) for the
  managed Node install path (`protoagent runtime install-node`, or the console card).

Installing a third-party **plugin** that declares an unbundled Python dependency your own
plugin code imports (`scope: host`) is refused on the frozen build rather than left to
fail at call time — see
[Install & publish plugins § dep scope](/guides/plugin-registry#dep-scope-which-interpreter-has-to-import-it)
for the exact rule and your options.

## 7. Safe recovery

Ordered least- to most-destructive — try each before moving to the next:

1. **The app won't open, or the window is blank/stuck.** Quit it completely (tray icon →
   Quit, or Task Manager) and relaunch from the Start Menu shortcut. If the window itself
   is misbehaving but you suspect the server underneath is fine, open
   `http://127.0.0.1:7870/app/` directly in a normal browser tab — that's the same console
   described in [Operator console (React/Tauri)](/guides/react-tauri-ui), reached without
   going through the shell window at all. If it loads there, the problem is the webview,
   not the agent or its data.
2. **Suspected port conflict.** You don't need to hunt down what's holding `7870` — the
   shell already probes it at launch and falls back to an OS-assigned free port if it's
   taken, so a stale/orphaned server from a previous run is usually harmless. If the
   in-app window seems to be pointed at a dead server, quitting and relaunching re-probes
   the port cleanly. (A port conflict from a *different* protoAgent instance — e.g. a
   source checkout also running on `7870` — is the same story: both bind fine, just on
   different ports.)
3. **Restart.** For anything that looks like a hung turn, a stuck tool call, or a plugin
   that misbehaved after an install, quitting and relaunching restarts the frozen server
   process cleanly — your data (§4) is untouched by a restart.
4. **Reconnect / re-run setup.** If the model endpoint or key needs to change, or a wizard
   answer needs revisiting, you don't need to reinstall — the Configuration drawer (or
   Settings ▸ re-run the wizard) covers every field the first-run wizard set; see
   [Spin up your first agent § Changing your mind](/tutorials/first-agent#changing-your-mind).
5. **Reinstall over kept data.** If the app itself seems broken in a way a restart doesn't
   fix, uninstall it (Windows Settings ▸ Apps, or the Start Menu entry) and run the latest
   `setup.exe` again. As covered in §4, this is non-destructive by design — your config,
   secrets, knowledge, and chat history in `%APPDATA%\studio.protolabs.protoagent\` are
   never touched by install or uninstall, so you land back exactly where you left off, on
   a clean binary.
6. **Last resort — back up, then reset.** If you suspect the *data* itself is the problem
   (a corrupt store, not the binary), back up before touching anything destructive:
   - Copy the whole `%APPDATA%\studio.protolabs.protoagent\` folder somewhere safe first.
     This is a full backup of the raw stores — broader than, and not a substitute for, an
     [agent snapshot](/guides/agent-snapshots) (which is a portable *recipe*: persona +
     config + plugin pins, deliberately excluding conversation history, memory, and every
     runtime sqlite store — export one too if you want a shareable, secret-free copy of
     just the agent's definition).
   - Only after that backup exists, close the app and rename (don't delete outright) the
     live folder, or remove the specific store under it that you believe is corrupt (e.g.
     `checkpoints.db` for chat/session state, `knowledge/` for the RAG store) — relaunching
     recreates whatever's missing from scratch, empty. This is destructive to whatever you
     remove; the backup is what makes it reversible.

## 8. Where to go next

- [Operator console (React/Tauri)](/guides/react-tauri-ui) — the console's full layout and surfaces
- [Plugins](/guides/plugins) · [Install & publish plugins (git URLs)](/guides/plugin-registry) — extend the agent without forking
- [Managed Python runtime (desktop)](/guides/python-runtime) — the `execute_code` / document-skill interpreter
- [Agent snapshots (export, share, duplicate)](/guides/agent-snapshots) — the portable recipe format, and its backup limits (§7)
- [Configuration reference](/reference/configuration) — every `langgraph-config.yaml` field
