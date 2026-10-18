# Ingestion API and configuration

For adding and verifying a source in the app, follow [Add documents and media](/guides/ingestion). API requests use the operator token when authentication is enabled.

## From the API

```bash
# a file
curl -H "Authorization: Bearer $OPERATOR_TOKEN" -F file=@notes.pdf -F domain=research \
  http://localhost:7870/api/knowledge/ingest

# a web page or YouTube URL
curl -H "Authorization: Bearer $OPERATOR_TOKEN" -F url=https://example.com/post -F title="That post" \
  http://localhost:7870/api/knowledge/ingest

# raw text
curl -H "Authorization: Bearer $OPERATOR_TOKEN" -F text="the thing to remember" -F title=Note \
  http://localhost:7870/api/knowledge/ingest
```

`POST /api/knowledge/ingest` (multipart) takes one of `file` / `url` / `text`, plus
optional `title` and `domain`. It responds with the chunk `ids`, the `chunks` count,
the detected `source_type`, and `chars`.

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

## Managed Python API

`GET /api/runtime/python` returns `{python, install}`:

| Key | Meaning |
|---|---|
| `needed` | this process would use it (frozen builds only) |
| `managed` / `managed_version` / `exe` | a working install is present, its version, its interpreter path |
| `baseline_installed` / `baseline_current` | document-library state vs the current pins |
| `supported` / `target_version` | can this platform/arch provision, and what an install fetches |

`POST /api/runtime/python/install` starts the provisioning in the background (`202`;
poll the GET for phase + percent). Unsupported platform/arch combinations return the
banner state instead — `execute_code` (and the skills behind it) can't run on that
desktop build.
