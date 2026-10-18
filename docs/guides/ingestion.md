# Ingest documents & media

Pull files, web pages, and media into the knowledge store so the agent can recall
them. Ingestion extracts text, splits it into chunks, and indexes it for search.
Embeddings are optional and disabled by default. One import
handles plain text, Markdown, HTML, PDF, Word (`.docx`), audio, video, and web/YouTube URLs.

> This is the *ingest* (write) side. For how recall works and the tuning knobs, see
> [Tune the knowledge store (RAG)](/guides/knowledge); for the design, see
> [Memory & the knowledge store](/explanation/memory-and-knowledge).

## From the console

**Knowledge → Store → Add source.** Drop a file, or paste a web/YouTube URL, pick a
**domain** (defaults to `general`), and import. The form accepts `txt, md, html, pdf, docx`
and audio/video (`mp3, wav, m4a, flac, ogg, opus, aac, mp4, mov, mkv, webm, avi, m4v`).
You’ll see `Added N chunks from "<title>"`.

Search **Knowledge → Store** for a distinctive word from the source, then ask a
question about it in a new chat. Check **Memory → Injections** to see what reached
the turn. Importing a source does not guarantee every passage enters every answer.
See [Check retrieval](/guides/knowledge#check-what-is-being-recalled).

For a failed import, read the error before retrying. Check the supported format,
required library or media model below, and whether a URL is reachable from the
agent’s machine. Scanned PDFs may lack extractable text. Remove unwanted imports
using [Manage memory](/guides/manage-memory#correct-a-memory).

## From the agent

The agent can ingest **on its own** with the `knowledge_ingest(source, domain, title?)`
tool — `source` is an `http(s)` URL (including YouTube) or a local file path. Hand it a
link or a file in chat ("read this and remember it", "ingest this PDF") and it runs the
same pipeline below, rather than trying to `web_search`/`fetch_url` a media link (which
can't get a transcript). It's distinct from `memory_ingest`, which only stores text the
agent already has — see the [tool table](/guides/knowledge#the-agent-s-memory-tools).

Audio/video/image paths need the same setup as the console (`knowledge.transcribe_model`
/ `knowledge.image_describe_model`, plus `ffmpeg` on PATH for video); if one isn't
configured the tool says so instead of failing silently.

**Slow sources run in the background so they never block the chat.** Anything that fetches
over the network (any URL, including YouTube) or transcribes media (audio/video) is detached
as a [background job](/adr/0050-background-subagents-reactive-notifications) — the tool
returns a job id immediately, and the result is delivered back into the conversation (and the
console's background jobs surface) when indexing finishes, so a 20-minute video never freezes
the turn. Only a small local text/Markdown file (≤64 KB) ingests inline. It's the same
durable background machinery as `task(run_in_background=true)`: concurrency-capped, cancellable,
and it survives past the turn. (With the background subsystem disabled via `BACKGROUND_DISABLED`,
the tool falls back to inline/blocking ingestion.)

## From the API

See [Ingestion API](/reference/ingestion#from-the-api) for multipart requests and responses.

## What's supported

| Source | How it's handled | Needs |
|---|---|---|
| Text / Markdown | decoded directly | — |
| HTML | readable text extracted (script/nav/footer stripped) | — |
| PDF | text extracted per page | `pypdf` |
| Word (`.docx`) | header, body, footer in document order — headings as `#` lines, list items as `- `, tables row by row | `python-docx` (a core dependency — every server, Docker and desktop install has it; a broken install missing it answers **501** naming the fix) |
| Web URL | fetched, then dispatched by content-type | — |
| YouTube URL | transcript via the captions API, else gateway STT | `youtube-transcript-api` |
| Audio | transcribed via the gateway's `/audio/transcriptions` (Whisper) | `knowledge.transcribe_model` set |
| Video | `ffmpeg` extracts the audio track → gateway STT | `ffmpeg` on PATH + `transcribe_model` |

Format is detected from the file extension, then the content-type, then a UTF-8
heuristic. The `pypdf` / `python-docx` / `youtube-transcript-api` deps are lazy-imported —
a missing one fails *that* source with a clear message, never the server. Legacy binary
Word (`.doc`) isn't readable: re-save it as `.docx` or export it to PDF, and the same goes
for a macro-enabled `.docm` or a password-protected file. A `.docx` is a zip of XML, and
nothing in it can be trusted about its own size, so the engine unpacks the text parts
itself in bounded steps and refuses (**413**) a document whose XML runs past ~12 MB or
~1M nodes (elements, attributes and text runs) — about twice a 300-page report. Parts that
carry no text (images, fonts, embedded objects) are never unpacked at all, and only a
couple of documents are extracted at a time, so one upload's ceiling can't be multiplied
by a burst of them.

Audio/video always transcribe **through the gateway** (no local ASR); leave
`transcribe_model` blank to disable media ingestion.

## Chunking & enrichment (config)

See [Ingestion configuration](/reference/ingestion#chunking-enrichment-config) for chunk sizes, enrichment, and media settings.
