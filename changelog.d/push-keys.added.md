- **Push security primitives: a per-instance VAPID keyring and push endpoint validation.**
  Groundwork for Web Push from the hub (ADR 0135 W2). `security.push_keys` generates this
  instance's own P-256 signing key on first use and keeps it in a versioned, owner-only
  keyring under the instance's `push` store — no vendor key, nothing shared between
  instances, nothing in config. Rotating activates a new version and keeps the old one
  signing for a 30-day migration window; retiring a compromised version removes it at once.
  A corrupt keyring, or one that has gone missing while devices are enrolled, raises a clear
  error instead of being quietly regenerated (which would silently stop every device's
  notifications). `validate_push_endpoint` admits only HTTPS endpoints on the supported
  Apple, Google and Mozilla push hosts and refuses credentials, odd ports, over-long URLs and
  hosts that resolve to private, loopback or link-local addresses, so enrollment cannot be
  used as an open relay. Nothing sends a push yet and no setting is added. `cryptography` is
  now a declared dependency (it was already installed transitively). Refs #5539.
