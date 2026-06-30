# Ingest documents & media

Pull files, web pages, and media into the knowledge store so the agent can recall
them. Ingestion **extracts text, chunks it, embeds it, and indexes it** — one call
handles plain text, Markdown, HTML, PDF, audio, video, and web/YouTube URLs.

> This is the *ingest* (write) side. For how recall works and the tuning knobs, see
> [Tune the knowledge store (RAG)](/guides/knowledge); for the design, see
> [Memory & the knowledge store](/explanation/memory-and-knowledge).

## From the console

**Knowledge → Store → Add source.** Drop a file, or paste a web/YouTube URL, pick a
**domain** (defaults to `general`), and import. The form accepts `txt, md, html, pdf`
and audio/video (`mp3, wav, m4a, flac, ogg, opus, aac, mp4, mov, mkv, webm, avi, m4v`).
You'll see `Added N chunks from "<title>"`.

## From the agent

The agent can ingest **on its own** with the `knowledge_ingest(source, domain, title?)`
tool — `source` is an `http(s)` URL (including YouTube) or a local file path. Hand it a
link or a file in chat ("read this and remember it", "ingest this PDF") and it runs the
same pipeline below, rather than trying to `web_search`/`fetch_url` a media link (which
can't get a transcript). It's distinct from `memory_ingest`, which only stores text the
agent already has — see the [tool table](/guides/knowledge#the-agents-memory-tools).

Audio/video/image paths need the same setup as the console (`knowledge.transcribe_model`
/ `knowledge.image_describe_model`, plus `ffmpeg` on PATH for video); if one isn't
configured the tool says so instead of failing silently. The tool is on whenever a
knowledge store is wired — the agent runs `knowledge_ingest` as one turn, so a long
recording will take as long as its transcription does.

## From the API

```bash
# a file
curl -F file=@notes.pdf -F domain=research \
  http://localhost:7870/api/knowledge/ingest

# a web page or YouTube URL
curl -F url=https://example.com/post -F title="That post" \
  http://localhost:7870/api/knowledge/ingest

# raw text
curl -F text="the thing to remember" -F title=Note \
  http://localhost:7870/api/knowledge/ingest
```

`POST /api/knowledge/ingest` (multipart) takes one of `file` / `url` / `text`, plus
optional `title` and `domain`. It responds with the chunk `ids`, the `chunks` count,
the detected `source_type`, and `chars`.

## What's supported

| Source | How it's handled | Needs |
|---|---|---|
| Text / Markdown | decoded directly | — |
| HTML | readable text extracted (script/nav/footer stripped) | — |
| PDF | text extracted per page | `pypdf` |
| Web URL | fetched, then dispatched by content-type | — |
| YouTube URL | transcript via the captions API, else gateway STT | `youtube-transcript-api` |
| Audio | transcribed via the gateway's `/audio/transcriptions` (Whisper) | `knowledge.transcribe_model` set |
| Video | `ffmpeg` extracts the audio track → gateway STT | `ffmpeg` on PATH + `transcribe_model` |

Format is detected from the file extension, then the content-type, then a UTF-8
heuristic. The `pypdf` / `youtube-transcript-api` deps are lazy-imported — a missing
one fails *that* source with a clear message, never the server. Audio/video always
transcribe **through the gateway** (no local ASR); leave `transcribe_model` blank to
disable media ingestion.

## Chunking & enrichment (config)

Large documents are split before embedding. Tune under `knowledge:` in
`langgraph-config.yaml`:

```yaml
knowledge:
  transcribe_model: whisper-1     # gateway STT model; blank disables audio/video
  chunk_max_chars: 1200           # target ceiling per chunk
  chunk_overlap_chars: 150        # shared tail between adjacent chunks
  chunk_min_chars: 200            # trailing fragments fold into the prior chunk
  contextual_enrichment: false    # prepend a 1-sentence doc context per chunk before
                                  # embedding (Anthropic Contextual Retrieval) — one aux
                                  # LLM call per chunk at INGEST time; off by default
  context_max_doc_chars: 12000    # doc cap fed to the enrichment prompt
```

Chat attachments take the same path above `attach_inline_budget` (default 8000 chars):
small files are read inline for the turn; larger ones are ingested + indexed and a lede
is surfaced, so a big paste never dumps into context. See
[chat file upload](/explanation/memory-and-knowledge).
