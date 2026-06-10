# Plugin console views (rail surfaces)

> [!IMPORTANT]
> This page moved. The single canonical guide for plugin views — rail surfaces, the
> `slot: "chat"` panel, the init/theme handshake, the event-bus bridge, the sandbox split, and the
> DS kit helpers — is now **[Building a plugin view](/guides/building-react-plugin-views)**.
>
> For the short copy-me quickstart, see **[Build a plugin view](/how-to/build-a-plugin-view)**.

A plugin can add its own **left-rail icon and view** to the operator console — a dashboard, board,
chart, editor, or a panel that *replaces* the built-in chat (`slot: "chat"`) — by declaring it in the
manifest and serving a page. **No console rebuild.** This is the frontend counterpart to
[plugin tools/routes](/guides/plugins); see [ADR 0026](/adr/0026-plugin-contributed-console-surfaces).

The mechanics — and the four rules every view should follow — live in the canonical guide:

→ **[Building a plugin view](/guides/building-react-plugin-views)**

Copy the gold-standard reference to start:

```bash
cp -r examples/plugins/chat_example plugins/
```
