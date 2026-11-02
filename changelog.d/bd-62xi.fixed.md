- **Image thumbnails in a user's chat bubble survive a rebuild from server state (#4700).**
  Attached-image thumbnails persisted only in the browser's local chat store, so opening the
  same chat in another browser profile or device — where the console rebuilds the transcript
  from durable server state (ADR 0104) — dropped them and fell back to the bare
  "Attached: `<name>`" text. The small bounded previews (the same data URLs, longest edge ≤ 320px
  — never the full image) now ride the durable user message and are restored onto the rebuilt
  bubble, capped at four thumbnails per message. Chats with no stored thumbnail (older chats, or
  a preview that failed) keep the "Attached: `<name>`" text fallback.
