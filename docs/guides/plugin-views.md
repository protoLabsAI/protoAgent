# Plugin console views (rail surfaces)

> [!IMPORTANT]
> This page moved. The single canonical guide for plugin views — rail surfaces, the
> `slot: "chat"` panel, the init/theme handshake, the event-bus bridge, the sandbox split, and the
> DS kit helpers — is now **[Building a plugin view](/guides/building-react-plugin-views)**.
>
> For the short copy-me quickstart, see **[Build a plugin view](/guides/build-a-plugin-view)**.

A plugin can add its own **left-rail icon and view** to the operator console — a dashboard, board,
chart, editor, or a panel that *replaces* the built-in chat (`slot: "chat"`) — by declaring it in the
manifest and serving a page. **No console rebuild.** This is the frontend counterpart to
[plugin tools/routes](/guides/plugins); see [ADR 0026](/adr/0026-plugin-contributed-console-surfaces).

The mechanics — and the four rules every view should follow — live in the canonical guide:

→ **[Building a plugin view](/guides/building-react-plugin-views)**

Follow the [view quickstart](/guides/build-a-plugin-view) to install the reference example in the active instance.

## Preview a plugin view without a host

To work on a view without a running protoAgent server, use the host-free view harness:

```
protoagent plugin preview <plugin-dir>
```

It serves the built operator console (`apps/web/dist` — run `npm run build --workspace
@protoagent/web` first; the harness never builds it silently) against the deterministic e2e
mock backend, and mounts your plugin's **real** routers through the test harness — so the view
page and its `/api/plugins/<id>/…` data routes answer for real, with no model, no network and no
host process. A router contract bug surfaces here, not only in production.

- `--port <n>` — the port the console mock serves on (default `4319`).
- `--open` — open the preview in your browser once it's up.
- `--layout <file.json>` — seed the UI layout, e.g. `{"surface": "main", "right": "side"}` to
  mount one view on the stage and another in the right panel at once. The harness writes it into
  the console's `protoagent.ui:<id>` localStorage key before the app boots.

The harness exposes the [event-bus](/adr/0039-plugin-event-bus) tap (publish a frame to the view,
and read back what it published) and adds a WebKit Playwright project — the desktop app runs
WKWebView, so a view that freezes only there (the two-mounts-plus-background-subscribe shape) is
now reproducible in CI. See [ADR 0034](/adr/0034-plugin-ui-first-class-react) and the fixture
under `apps/web/e2e/fixtures/preview-fixture`.
