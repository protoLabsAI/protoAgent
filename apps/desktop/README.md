# protoAgent Desktop

Tauri v2 wrapper for the React operator console.

## Commands

```bash
# 1. Freeze the server into the bundled sidecar (per platform).
#    Needs a venv with the runtime deps + PyInstaller:
#      pip install -r requirements.txt pyinstaller
npm run desktop:sidecar

# 2. Build the React app + native bundle (expects the sidecar from step 1).
npm run desktop:build

# Dev (also needs the sidecar binary present from step 1):
npm run desktop:dev
```

`desktop:build` builds the React app with relative asset paths, then produces the native bundle under `apps/desktop/src-tauri/target/release/bundle/`.

## Runtime Model

The app **bundles and launches the protoAgent server itself** as a Tauri sidecar — no separately-running server required.

- `apps/desktop/sidecar/build_sidecar.py` freezes the server into a single binary via PyInstaller, named `binaries/protoagent-server-<target-triple>` (the `externalBin` Tauri bundles). The React console is the UI, so the binary stays lean (~60 MB) rather than carrying a heavier server-rendered UI stack.
- On launch the Rust shell (`src-tauri/src/lib.rs`) spawns the sidecar on the **fixed port 7870** with `--ui console --port 7870` (the console UI tier — API + A2A + console; ADR 0010), sets `PROTOAGENT_CONFIG_DIR` to the per-user app-config dir (so the read-only binary still persists setup/secrets), drains its output to the log, and kills it on app exit. (The dynamic-free-port + window-injection handoff proved unreliable across Tauri v2 webview contexts, so the port is pinned to the web client's fallback — see the comment in `lib.rs`.)
- The shell creates the window itself and injects `window.__PROTOAGENT_API_BASE__` (the chosen `http://127.0.0.1:<port>`) before any page script runs; the webview's React build reads it (`apps/web/src/lib/api.ts`) and calls the sidecar's `/api`, `/a2a`, and `/v1`. The console probes with backoff on startup so the few-second cold start doesn't surface as an error.

The sidecar binary is gitignored — it's a build artifact produced per platform by step 1 (locally or in CI before `tauri build`).

To point the desktop UI at a *different* server instead of the bundled one, set `protoagent.apiBase` in localStorage (it wins over the injected port).

## Desktop Behavior

- Tray menu: show, hide, quit.
- Close button hides the window instead of quitting.
- `Cmd+Shift+P` on macOS or `Super+Shift+P` on Linux/Windows toggles the window.

## Platforms & CI

`.github/workflows/desktop-build.yml` builds all three platforms on semver tags
(and on manual dispatch, which uploads workflow artifacts instead):

| Platform | Artifact | Signing |
|---|---|---|
| macOS (aarch64) | `.dmg` | Developer ID, signed + notarized (full Apple secret set) |
| Linux (x86_64) | `.AppImage` + `.deb` | unsigned |
| Windows (x86_64) | NSIS `-setup.exe` | unsigned — expect a SmartScreen prompt until a Windows signing identity is added |

Notes per platform:

- **Every leg smoke-tests the frozen sidecar** before bundling: `scripts/live_smoke.py --bin`
  boots the actual PyInstaller binary (neutral cwd, no `PYTHONPATH`) and drives a real A2A
  turn, so per-platform under-collection fails CI rather than the first launch on a user's
  machine.
- **Linux** builds on `ubuntu-22.04`, so the frozen sidecar needs glibc ≥ 2.35 at runtime
  (PyInstaller binaries don't run on older glibc than they were built with). The tray icon
  needs `libayatana-appindicator3-1` — declared as a `.deb` dependency; the AppImage bundles it.
- **Windows** PyInstaller onefile binaries are occasionally false-flagged by AV — a known
  PyInstaller issue; code-signing the sidecar/installer is the durable fix.
- The real release version is stamped into `tauri.conf.json` at build time (in-tree it stays
  a placeholder), so the installer/app metadata reports the actual `pyproject.toml` version.
