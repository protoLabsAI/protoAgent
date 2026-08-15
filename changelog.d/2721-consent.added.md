- **The one-time "this runs code" consent is real (ADR 0071 D3 S4–S6, #2721).**
  Installing from a source that is neither official (`plugins.sources.official`,
  fork-overridable) nor previously acked now answers `needs_ack` — before anything is
  fetched — and the console asks with a proper confirm: the install-by-URL dialog and
  the Discover one-click path (which previously had **no confirm at all**) share the
  new TrustAckDialog; confirming persists the exact repo into `plugins.sources.acked`
  via `POST /api/plugins/ack`, and "don't ask again" flips `plugins.trust_unverified`.
  Fetch-only installs (`PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1`) skip the gate — no
  code runs, nothing to consent to yet. The docstrings #2720 corrected now describe a
  dialog that actually exists.
