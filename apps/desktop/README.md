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
- On launch the Rust shell (`src-tauri/src/lib.rs`) **picks a free port**, spawns the sidecar with `--ui console --port <port>` (the console UI tier — API + A2A + console; ADR 0010), sets `PROTOAGENT_CONFIG_DIR` to the per-user app-config dir (so the read-only binary still persists setup/secrets), drains its output to the log, and kills it on app exit. A free port (not a fixed 7870) means the desktop coexists with any other agent already running — and is the foundation for several agents at once.
- The shell creates the window itself and injects `window.__PROTOAGENT_API_BASE__` (the chosen `http://127.0.0.1:<port>`) before any page script runs; the webview's React build reads it (`apps/web/src/lib/api.ts`) and calls the sidecar's `/api`, `/a2a`, and `/v1`. The console probes with backoff on startup so the few-second cold start doesn't surface as an error.

The sidecar binary is gitignored — it's a build artifact produced per platform by step 1 (locally or in CI before `tauri build`).

To point the desktop UI at a *different* server instead of the bundled one, set `protoagent.apiBase` in localStorage (it wins over the injected port).

## Desktop Behavior

- Tray menu: show, hide, quit.
- Close button hides the window instead of quitting.
- `Cmd+Shift+P` on macOS or `Super+Shift+P` on Linux/Windows toggles the window.
