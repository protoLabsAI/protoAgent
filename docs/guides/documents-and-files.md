# Work with documents and files

You can attach files to a chat, ask the agent to create or revise documents, and
download the results. Document creation needs the bundled Cowork skills and
`execute_code`; both are enabled by default. On desktop, install the
[Python runtime](/guides/python-runtime) before creating office documents.

## Attach a file and ask a question

1. Use the composer's attachment control or drag a file into chat. You can also
   paste an image from the clipboard.
2. Wait for the attachment to finish uploading. If its chip reports an error,
   remove it or retry before sending the request.
3. Ask a concrete question, such as:

   > Summarize this report's recommendations and cite the relevant pages.

4. Read the answer against the source. For scanned or image-based material,
   recognition depends on the configured image or extraction capabilities.

Attaching sends content to the agent's server; relevant content can then go to
the selected model. For a document collection you want to manage as reference
material, use [Knowledge → Store → Add source](/guides/ingestion).

## Create a document

Choose a writable work folder in **Settings → Tools → Filesystem → Work folders**.
Use a folder on the agent server's machine. A remote agent cannot save directly
to a path that exists only on your laptop; download its result instead.

Ask for the format, content, and destination:

> Create a one-page Word document titled “Weekly plan,” with three priorities
> and a checklist. Save it as weekly-plan.docx in my work folder and offer a download.

You can also choose `/docx`, `/xlsx`, `/pptx`, or `/pdf` from the slash menu and
add your instructions. Specify the data or source material the document should
use rather than expecting the format skill to supply it.

When creation succeeds, the agent should report the saved file and show a file
artifact in chat. Open its chip or the **Artifact** panel, then choose
**Download** from its controls. Supported files can preview as document pages,
slides, or tables in the panel. If a preview falls back to extracted text, download
the file to inspect it in its usual app. Check the content and formatting.

The work-folder file lives on the server. The downloaded copy lives on the
computer or phone where you opened the console. Keep the copy you need before
closing or cleaning up the task.

## Revise a result

Ask for a specific edit to the existing document:

> In the same weekly plan, move the testing checklist before the release
> checklist. Keep the rest of the document and return an updated download.

A file artifact can keep multiple versions. Select the version you want before
downloading it. Ask the agent to revise the same artifact when you want changes
in that version chain. Existing artifacts and versions can be evicted under
the configured history limits, so download deliverables you need to keep.

A file artifact is a stored copy: deleting it does not delete the original file
in your work folder. Back up work folders separately from
[app data](/guides/backup-and-restore).

## Recover from a failed document task

| Error or symptom | Check |
| --- | --- |
| Missing Python runtime | **Settings → Tools → Install runtime** |
| Stale document libraries | **Settings → Tools → Update runtime** |
| File or module missing in a source/package run | Enable the Cowork and execute_code plugins and install their document dependencies in that Python environment |
| Cannot read or write the destination | The server's work-folder path and its write permission |
| File saved but no download card | Ask the agent to offer the existing file as a file artifact; check that the artifact plugin is enabled |
| Download blocked or failed | Read the error, retry the download, or open the console in a browser on the same device |

Inspect the failed tool card before asking it to repeat the task. A stopped or
failed turn may already have written a file. Follow
[Troubleshooting](/guides/troubleshooting#documents-fail) for runtime and file-access repairs.
