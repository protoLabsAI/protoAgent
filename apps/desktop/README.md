# protoAgent Desktop

Tauri v2 wrapper for the React operator console.

## Commands

```bash
# 1. Freeze the headless server into the bundled sidecar (per platform).
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

- `apps/desktop/sidecar/build_sidecar.py` freezes the server (`server.py --headless`) into a single binary via PyInstaller, named `binaries/protoagent-server-<target-triple>` (the `externalBin` Tauri bundles). Gradio is excluded — the React console is the UI — so the binary is ~55 MB rather than carrying the full UI stack.
- On launch the Rust shell (`src-tauri/src/lib.rs`) spawns the sidecar with `--headless --port 7870`, sets `PROTOAGENT_CONFIG_DIR` to the per-user app-config dir (so the read-only binary still persists setup/secrets), drains its output to the log, and kills it on app exit.
- The webview loads the bundled React build and calls the sidecar's `/api`, `/a2a`, and `/v1` on `127.0.0.1:7870`. The console probes with backoff on startup so the few-second cold start doesn't surface as an error.

The sidecar binary is gitignored — it's a build artifact produced per platform by step 1 (locally or in CI before `tauri build`).

To point the desktop UI at a *different* server instead of the bundled one, set `protoagent.apiBase` in localStorage to the desired base URL.

## Desktop Behavior

- Tray menu: show, hide, quit.
- Close button hides the window instead of quitting.
- `Cmd+Shift+P` on macOS or `Super+Shift+P` on Linux/Windows toggles the window.
