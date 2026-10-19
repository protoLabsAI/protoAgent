# First-wave production sources

All 28 records in `../registry.json` now point to reviewed exports. Per-asset
JSON files retain the procedure, demo conditions, evidence, and privacy review.
The registry owns placements and freshness fields; do not duplicate them here.

- `diagrams.json` and `render_diagrams.py` generate the eight editable SVGs and
  their published copies. Run the script from any working directory.
- `capture_fleet_deck.py` connects to a real isolated hub and exports its actual
  terminal roster. Start the demo members and a real pending human question
  first. The published PNG uses the retained SVG with local monospace fonts;
  it does not depend on a font CDN.
- `trace_collector.py` is a local OTLP evidence receiver, not a Langfuse server.
  It parses SDK-exported protobuf spans and stores IDs, names, service names,
  timing and status. `trace-correlation-spans.json` is the captured evidence;
  `trace-correlation.html` is the editable visualization of those exact IDs.
- App captures use real model responses and synthetic data. The phone image
  combines two real post-pairing screenshots, with phone emulation identified.
  No usable pairing code or credential appears in a published asset.
- Document and memory assets reuse the reviewed marketing capture cited in
  their source notes. The document poster is lossless WebP; the clip is unchanged.

Reproduction scripts use disposable localhost ports and `/tmp` paths. Check
that ports are free, follow the repository's box/instance isolation rules,
and revoke demo devices and stop demo agents when finished. Do not run a
capture against a production workspace just to reproduce the appearance.
