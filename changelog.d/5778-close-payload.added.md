- **Push: a close payload lets a phone drop a settled notice's notification without showing a new one.**
  `build_push_payload` now builds a versioned close payload, `{"v": 1, "type": "close", "tag": "<notice id>"}`,
  carrying the tag only (no text, no link) and refusing any tag that is not an opaque notice id. On a valid
  close push the console's push worker closes every notification shown under that tag and shows nothing; a
  malformed close push shows the generic fallback as before, and normal notice pushes are unchanged. The
  worker's header comment documents why iOS Safari web push must not be sent a close push. The hub does not
  send close pushes yet; that is the next slice of #5778.
