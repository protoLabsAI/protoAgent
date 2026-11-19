- **A paired device's push subscription now follows that device's own lifecycle.** Rotating
  a device's token keeps its Web Push binding and moves it to the next generation, so a send
  queued under the old generation is dropped. A handoff to another origin moves nothing: the
  binding is cleared when the handed-off token is retired (the new origin's token is first
  used), and an offer nobody completes leaves the old origin's binding alone. Unpairing and
  a reuse-detection revocation delete the binding with the row, so a binding write that
  arrives afterwards is refused, and a device idle past `devices.idle_ttl_days` loses its
  binding where it loses its pairing. A binding enrolled under a VAPID key version that was
  retired, or that has left its 30-day migration window, is cleared too, and
  `GET /api/devices/self/push` now returns `renew` (`vapid_key`, `handoff` or `idle`) so the
  device can subscribe again. All of it runs through the existing device registry paths
  (ADR 0121); there is no second expiry mechanism, and nothing sends a push yet. Refs #5539.
