- **Chat messages now show when they were sent (#3448).** A settled user or assistant
  message carries a quiet clock chip in its footer with the local time it was sent; hover
  or keyboard-focus it to see the full local date and time. It shows in both the main chat
  and the command-palette chat (they share one renderer), stays out of the way like the
  existing usage/cost footer, and simply doesn't appear on a message with no recorded send
  time or while a reply is still streaming — so there's never a wrong or `Invalid Date`
  value. Background, scheduled, and server-result cards keep their own timestamp treatment.
