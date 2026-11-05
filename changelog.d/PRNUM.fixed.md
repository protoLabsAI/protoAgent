- **The console's remaining copy buttons say when a copy didn't land (#PRNUM).**
  #4737 fixed the trace and background-job copy buttons. Five more still showed "Copied" or a
  "copied" toast without checking the write, and two of them sit on the device pairing screen,
  where a silent failure means a lost token. Chat message copy, the prompt viewer, the run
  drawer, and the device token and pairing code buttons now go through `copyText` and report a
  failure when the clipboard is unavailable (plain-http access) or the write is rejected. The
  code viewer's two copies, which already handled failure, use the same helper.
