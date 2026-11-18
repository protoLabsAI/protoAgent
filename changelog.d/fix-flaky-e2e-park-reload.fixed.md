- **A turn that parks on a question right after you send it comes back after a reload.**
  The console saved a turn's task id on the same two-second delay it uses for streamed text, so
  a reload, or a closed tab, in those first two seconds found a reply still running with no task
  to reattach to. The question's form never came back. The task id is now saved the moment it
  arrives. Three end-to-end tests that failed at random on CI also wait for the real condition
  now: the saved task ids before a reload, the open event stream before an answer arrives from
  elsewhere, and the form being on screen before the test server drops the connection. The PDF
  Worker timing check now measures only parsing and drawing, so it no longer counts the file
  transfer, which costs the same on both paths.
