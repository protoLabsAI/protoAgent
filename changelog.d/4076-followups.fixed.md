- **The Artifact panel follows only the active chat's artifacts (#4076).** A panel artifact made by
  another chat tab, by a `/v1`, A2A or ACP client, or saved before chats were recorded no longer
  takes the panel over. It stays in the picker, and **Open in panel** still shows it. The console
  now tells the panel which chat tab is active, so switching tabs moves the panel to that chat's
  newest panel artifact.
- **"Open in panel" never opens a 0 px panel (#4076).** A right or bottom dock stored at 0 px
  (left that way by a drag-to-collapse) reopens at its default size.
- **Inline artifacts no longer stick at 80 px with a clipped heading (#4076).** A frame whose
  content settled late inside a viewport-sized wrapper (`height: 100vh`) kept its first, too-early
  measurement. The frame now re-measures as the content and fonts settle, and sizes a
  viewport-sized layout to its content.
