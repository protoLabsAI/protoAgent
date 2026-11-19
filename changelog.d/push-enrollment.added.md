- **Push enrollment for paired phones and browsers, stored on the device's own pairing record.**
  A paired device can now fetch this agent's VAPID public key and store, renew or delete its
  own Web Push subscription through `GET` / `PUT` / `DELETE /api/devices/self/push` (ADR 0135
  W2). The subscription is a field of the device's row in the existing device registry, not
  a second store: a token rotation keeps it, and unpairing the device (or a reuse-detection
  revocation) deletes it in the same write, so a revoked device's pushes stop with it. The
  routes are self-only: the device is the one the caller's token resolves to, so no caller
  can read or change another device's subscription, and the shared auth token, an agent
  pairing and unauthenticated callers are refused. An endpoint must pass
  `validate_push_endpoint`, the client keys must be a valid P-256 point and a 16-byte secret,
  and the key version must be the active one or still inside its migration window; a refusal
  stores nothing. No response returns the endpoint or the client keys. Nothing sends a push
  yet, and there is no new setting or console UI. Refs #5539.
