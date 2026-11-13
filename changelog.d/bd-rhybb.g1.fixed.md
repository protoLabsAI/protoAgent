- **A delivery's Work detail titles the page with a short summary, not the whole message (bd-rhybb).**
  An undelivered page's detail rendered the entire message — paragraphs of it — as the
  bold heading, because the detail passed the raw page text straight into the title.
  The heading (and the Needs-you row title) is now the first sentence of the first
  line, whitespace collapsed, clipped to ~80 characters with an ellipsis — the same
  rule schedules already apply to a prompt, via one shared `summaryTitle` helper. The
  full text shows once, under the title, in a new Message field rendered as body text
  (wrapping, line breaks preserved); the From / Waiting since / Status fields and the
  Dismiss action are unchanged, and a page with an empty text keeps the "Delivery"
  fallback title.
