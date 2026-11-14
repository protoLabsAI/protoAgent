- **A federation-token A2A caller cannot approve or bypass a gated `run_command`, now proven end to end (#5162).**
  The trust-tier approval authority from #5138 — a federation credential may not answer an approval
  card or skip the gate with `bypass_permissions` — is now covered by a test that drives a real
  `/a2a` `SendMessage` through the auth middleware and the streaming turn driver into the live gate:
  the federation caller is refused the command, while the operator credential runs the same request
  past the gate.
