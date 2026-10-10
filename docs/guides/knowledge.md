# Tune knowledge recall

The agent can recall saved facts and documents as context for its answers.
Keyword search works by default. Start with useful sources and check retrieval
before changing its limits.

To edit or remove a fact, review pending memories, or use incognito, follow
[Manage memory](/guides/manage-memory). To add material, follow
[Ingest documents and media](/guides/ingestion).

## Check what is being recalled

1. Open **Knowledge → Store** and search for a distinctive word from the source.
   Turn off the clipboard-check **pending review filter** to search all entries.
2. Read the matching chunks. Correct inaccurate material or remove unrelated
   sources before increasing retrieval limits.
3. Ask a question in a new chat that should use the source. Check
   **Memory → Injections** to see which entries the turn actually received.
4. Change one setting in **Settings → Knowledge**, then **Save & apply** and
   try the question again.

An empty search can mean the source was not ingested, the search wording does
not match, or a filter excludes it. Ordinary keyword search matches whole
words: try `Postgres` rather than `postg`. The command palette also matches
the start of the word you are typing.

## Match different wording {#which-store-you-get}

Enable hybrid search when questions use different wording from your sources:

1. Confirm your gateway serves an embedding model. This is a separate model
   from the one used for chat.
2. In **Settings → Knowledge → Recall**, set **Embedding model** to that
   model's name and enable **Semantic recall (embeddings)**.
3. **Save & apply**, then repeat a question whose wording differs from the
   source. Compare the injected entries.

Hybrid search combines keyword and semantic matches. If embedding requests
fail, keyword search remains available. Check the model name, gateway
credentials, and agent logs; the embedding retry cooldown defaults to five
minutes. A successful chat-model test alone does not verify the embedding
endpoint.

## Adjust context and relevance {#the-knobs}

| Symptom | Setting to try |
| --- | --- |
| Too few useful sources reach the turn | Increase **Knowledge recall top-k** from its default of `5` |
| Hybrid search misses a relevant source | Increase **Hybrid candidate pool** (default `20`) |
| Unrelated matches keep appearing | Increase **Recall relevance floor** gradually and compare results; scores depend on the search mode |
| A useful match is cut off | Increase **Recall preview length** from its default of `1000` characters |
| Too much automatic context enters each turn | Lower **Injected-context budget (% of model window)** from its default of `8` |
| Previous chats distract from the current task | Change **Prior-session digest** to `relevant` or `off` |

All these controls are in **Settings → Knowledge → Recall**.

Setting `top_k: 0` stops automatic document-hit injection. It does not disable
tool-driven recall or the other memory types. Similarly, `prior_sessions: off`
stops the automatic past-session digest; the agent can still search sessions
on demand.

The context budget sheds document hits first, then past-session entries, then
skill descriptions. It keeps working state and always-on memory, and has a
minimum size, so lowering the percentage does not remove all automatic
context. Use the [configuration reference](/reference/knowledge#the-knobs)
for these bounds and advanced tuning.

## Control automatic memory delivery {#memory-delivery-controls-adr-0069}

<span id="scope-the-auto-inject-to-namespaces"></span>
<span id="trust-tiers-adr-0069-d8"></span>
<span id="hot-memory-write-visibility-adr-0069-d8"></span>

Advanced settings can restrict automatic document recall to named namespaces,
set a minimum source trust tier, or require operator-written always-on memory.
These controls do not disable every way the agent can read or write knowledge.
See [delivery controls](/reference/knowledge#memory-delivery-controls-adr-0069)
for each setting's scope.

## Manage saved content

<span id="incognito-threads"></span>
<span id="conversations-in-memory-and-deleting-a-chat"></span>
<span id="the-per-turn-injection-record"></span>
<span id="the-memory-inspector-console"></span>
<span id="staleness-supersede-dont-delete"></span>

- [Inspect a turn](/guides/manage-memory#inspect-a-turn).
- [Correct, reject, or delete a memory](/guides/manage-memory#correct-a-memory).
- [Delete a chat and its saved memory](/guides/manage-memory#delete-a-chat).
- [Use incognito](/guides/manage-memory#use-incognito).

## Advanced reference

<span id="the-agent-s-memory-tools"></span>
<span id="plugin-knowledge-lifecycle"></span>
<span id="sharing-knowledge-across-a-fleet-the-commons"></span>

- [Knowledge settings and tools](/reference/knowledge).
- [Plugin lifecycle operations](/reference/knowledge#plugin-knowledge-lifecycle).
- [Fleet commons](/reference/knowledge#sharing-knowledge-across-a-fleet-the-commons).
- [Memory storage and delivery](/explanation/memory-and-knowledge).
