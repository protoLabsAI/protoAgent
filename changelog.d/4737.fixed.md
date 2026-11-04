- **Console copy and dismiss failures say so instead of claiming success (#PR).**
  Copying a trace id in Recent turns toasted "Trace id copied" even when the clipboard
  write failed, and the background agents dialog's Copy button left an unhandled
  rejection when the Clipboard API was missing or refused. Both now report success only
  once the write lands and say the copy failed otherwise; the trace-id toast carries the
  id so it can still be selected. Stopping, deleting or clearing background jobs updated
  the list right away and ignored a server error, so a refused action looked done. The
  list now rolls back and a toast names what failed.
