# Build and test the console

Build the React console from a source checkout, develop against an isolated
backend, or package the Tauri desktop app. You need Python 3.11+, uv, Node 20,
and npm 11+. For native packaging, also install Rust and the platform's Tauri
build prerequisites.

## Build and serve

From the repository root:

```bash
uv sync --frozen
# With nvm installed:
nvm use
# If npm --version is below 11:
npm install -g npm@11
npm ci
npm run build --workspace @protoagent/web
uv run python -m server
```

The build writes `apps/web/dist/`. The server serves it at `/app` and redirects
`/` there. A checkout has no prebuilt console: rebuild after frontend changes.
If the directory is missing, the server warns and serves only the API.

`--ui none` skips the console. `full` is a deprecated alias for `console`.

## Develop with live reload

Run these in separate terminals at the repository root:

```bash
scripts/dev.sh
```

```bash
npm run dev --workspace @protoagent/web
```

Vite serves port 5173 and proxies backend requests to port 7871 by default. The
dev server uses `~/.protoagent/dev/` for instance data and inherits the machine's
shared model defaults. `PROTOAGENT_API_BASE` overrides the proxy target.

For a throwaway review server that also needs separate machine-wide state, set
both roots before starting it:

```bash
PROTOAGENT_BOX_ROOT=/tmp/pa-review PROTOAGENT_INSTANCE=review \
  uv run python -m server --port 7881
```

A fresh box root has no model credentials; configure a model if the review needs
chat. Point Vite at `http://127.0.0.1:7881` for that instance.

## Test

```bash
npm run test:unit --workspace @protoagent/web
npm run test:e2e --workspace @protoagent/web
```

The Playwright suite builds the SPA and serves it against a deterministic mock
backend. Add fixtures and a spec for a new console behavior. The console holds a
long-lived event stream; use `waitUntil: "load"` rather than `networkidle` in tests.

Test interaction changes in the desktop app too. Its webview differs from
Playwright's Chromium. Every desktop `WebviewWindowBuilder` must keep
`.disable_drag_drop_handler()` so HTML drag-and-drop reaches the page.

## Package the desktop app

Install PyInstaller into the Python environment, then build the sidecar before
the native bundle:

```bash
uv pip install pyinstaller
uv run npm run desktop:sidecar
npm run desktop:build
```

`npm run desktop:dev` also needs a built sidecar. Bundles appear under
`apps/desktop/src-tauri/target/release/bundle/`.

The sidecar freezes the Python server and bundled plugins. Runtime-installed
plugins can import only libraries available in that build or provided by the
managed dependency installer. Desktop chat streams A2A through a native relay
and falls back to `/api/chat` if the relay fails.

For release artifacts, signing, and updater publication, follow
[Releasing](/guides/releasing#desktop). Repository-specific platform checks and
build rules live in [PROTO.md](https://github.com/protoLabsAI/protoAgent/blob/main/PROTO.md).
