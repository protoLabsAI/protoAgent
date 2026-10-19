# Memory and knowledge

An agent can use this chat, saved facts, previous-conversation summaries, and
reference documents when answering. These have different lifetimes and controls.
Saving something does not guarantee it will appear in every answer.

## What the agent remembers

| Material | How it is used | Where to inspect it |
| --- | --- | --- |
| Current conversation | Context for subsequent turns; long chats can be compacted | The chat and its tool cards |
| Past-session summaries | A digest can point the agent to previous conversations | **Memory → Sessions** |
| Standing preferences (hot memory) | Eligible for automatic delivery each turn, within delivery limits | **Memory → Hot memory** |
| Saved facts and documents | Relevant passages can be retrieved for a question | **Knowledge → Store** |
| Skills | Procedures the agent can load when relevant | **Settings → Skills** |

[Attaching a file](/guides/documents-and-files) gives the current chat material
to work with. [Adding a knowledge source](/guides/ingestion) keeps reference
material for later recall. A work-folder file and a downloaded artifact are
separate copies; neither is a complete backup of the agent.

<!-- docs-media: memory-lifecycle -->
<figure class="docs-media"><img src="/media/memory-lifecycle/v1.svg" alt="Current-chat material, saved references, and memory have different controls. Attach a file → Use in this conversation → Inspect chat + tool cards; Ingest a source → Save in Knowledge → Store → Retrieve relevant passages; Save a memory → Eligible for later turns → Inspect Memory → Injections" loading="lazy" style="max-width:100%;height:auto"><figcaption>Inspect, correct, or remove saved material in Memory and Knowledge. Saving does not guarantee delivery.</figcaption></figure>
<!-- /docs-media -->

## Why an answer used—or missed—a source

Automatic context includes selected past sessions, standing memory, and retrieved
knowledge. Search is keyword-based by default. Optional embeddings help match
different wording, but require a working embedding endpoint.

The context budget limits how much enters a turn. Retrieved hits can be dropped
before past-session entries and skill descriptions; standing context has its own
limits. The agent can also use tools to retrieve more material during the turn.

Open **Memory → Injections** to inspect what a particular turn received, then
check its tool cards for later reads. Use [Tune knowledge recall](/guides/knowledge)
to test a known source before changing retrieval settings.

## Correcting or removing information

Use [Manage memory](/guides/manage-memory) to edit a fact, reject an entry, remove
an imported source, or delete a conversation and its derived memory. Pending
entries can still be delivered; rejecting an entry stops its future delivery.
Editing saved memory does not remove text already present in an open chat.

Deleting a chat, deleting a summary, and deleting a saved fact are different
actions. Incognito skips automatic memory operations but is not a promise that
nothing is stored: the chat persists, messages reach the selected model, and
explicit tools, plugins, and delegates have their own operations. Follow
[Use incognito](/guides/manage-memory#use-incognito) for the exact limits.

Knowledge is private to the agent by default. A fleet can opt into a shared
commons; [sharing a fact](/guides/manage-memory#share-a-fact) explains the controls.
Use a [data backup](/guides/backup-and-restore) to preserve the complete local state.

## Technical details

The storage and delivery contract is in [Memory internals](/reference/memory-delivery).

<a id="the-store"></a>
<a id="three-kinds-of-memory"></a>
<a id="write-paths"></a>
<a id="the-reasoning-guardrail"></a>
<a id="facts-dedup-supersede-deterministically"></a>
<a id="write-lifecycle-adr-0108-d7"></a>
<a id="delivery-what-enters-the-prompt-adr-0069"></a>
<a id="one-projection-for-every-runtime-adr-0108-d8"></a>
<a id="delivery-budget-and-priority-adr-0108-d6"></a>
<a id="trust-tiers"></a>
<a id="incognito-threads"></a>
<a id="forensics-the-injection-log"></a>
<a id="semantic-recall-embeddings"></a>
<a id="configuration"></a>
<a id="see-also"></a>

See the [technical contract](/reference/memory-delivery) for the store schema, write lifecycle, delivery budget, trust tiers, and injection log.

<a id="memory-the-knowledge-store"></a>
