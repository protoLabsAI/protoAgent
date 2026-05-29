# protoAgent Desktop

Tauri v2 wrapper for the React operator console.

## Commands

```bash
npm run desktop:dev
npm run desktop:build
```

`desktop:build` builds the React app with relative asset paths, then produces the native bundle under `apps/desktop/src-tauri/target/release/bundle/`.

## Runtime Model

This wrapper is in connect-to-local-server mode. The packaged UI expects a protoAgent server at `http://127.0.0.1:7870` and calls its `/api`, `/a2a`, and `/v1` routes from the desktop webview. Sidecar bundling can replace that requirement later.

To point the desktop UI at a different server, set `protoagent.apiBase` in localStorage to the desired base URL.

## Desktop Behavior

- Tray menu: show, hide, quit.
- Close button hides the window instead of quitting.
- `Cmd+Shift+P` on macOS or `Super+Shift+P` on Linux/Windows toggles the window.
