# Inspect, correct, and remove memory

Use **Memory** to inspect context the agent receives automatically, and
**Knowledge → Store** to search and edit saved facts and documents. Open either
from the rail or by typing its name in the [command palette](/guides/command-palette).

| I want to… | Where to go |
| --- | --- |
| See what a particular turn received | **Memory → Injections** |
| Read or remove a past conversation summary | **Memory → Sessions** |
| Correct a standing preference or instruction | **Memory → Hot memory** |
| Find, review, edit, or delete a saved fact | **Knowledge → Store** |
| Add reference files or pages | [Ingest documents and media](/guides/ingestion) |
| Change how much context is recalled | [Tune recall](/guides/knowledge) |

## See what the agent used {#inspect-a-turn}

1. Open **Memory → Injections**.
2. Filter by the chat's session ID, or use a session row's syringe button in
   **Memory → Sessions** to open its injection history.
3. Click a turn to open **What this turn used**. Inspect the past conversations,
   memories, and documents listed there.

This records automatic memory delivery. Tool calls can retrieve more material
afterward; inspect the turn's tool cards for those reads. An old injection
record may refer to an entry you have since deleted.

## Correct or remove a memory {#correct-a-memory}

For a standing preference, open **Memory → Hot memory**, choose the pencil
beside the entry, edit its text, and **Save**. These entries are eligible to
appear on every turn. A **not injecting** badge means the entry is absent
from the current delivery window: it may be rejected, expired, or outside the
hot-memory budget.

For another saved fact, open **Knowledge → Store**, search for it, choose
**Edit entry**, and save the correction. To remove a single entry, choose its
trash button and confirm **Delete entry**. A single-entry delete cannot be
undone. Changes affect future delivery; text already in a chat remains in that
chat's history. Start a new chat to check the correction without that old context.

A document can have several stored chunks. Use the source group's trash
button to delete the whole ingest, then confirm **Delete chunks**. The toast
offers **Undo** for this bulk deletion; use it immediately if you chose the
wrong source.

## Review what the agent saved {#review-memories}

In **Knowledge → Store**, turn on the clipboard-check **pending review filter**.
Inspect an entry's wording and source before choosing a review action:

| Action | Result |
| --- | --- |
| Confirm (check mark) | Marks the memory as reviewed |
| Reject (cross) | Keeps the row for inspection and stops delivering it to the agent |
| Re-open (return arrow) | Returns a rejected entry to pending; it becomes eligible for delivery again |

**Pending entries can still be delivered.** Review status is not an approval
queue that blocks all unconfirmed content. Reject or delete an entry to stop
using it. The same review controls appear for hot-memory entries. Editing an
entry confirms the new revision, except that a rejected entry stays rejected.

## Remove a past conversation summary {#remove-a-summary}

Open **Memory → Sessions**, click a row to read its summary, then use the
trash button and confirm **Delete summary**. It leaves the automatic
past-conversation digest and summary recall within about a minute.

Deleting a summary does not delete the original chat or facts and documents
already saved in **Knowledge → Store**. Remove those separately if needed.
A conversation that continues can produce a new summary later.

## Delete a chat and its saved memory {#delete-a-chat}

Use the chat's delete action, or run `/clear` to empty it while keeping its tab.
The confirmation dialog offers these choices:

- **Harvest into the knowledge base first** saves a searchable summary and
  extracted facts before deleting the chat. It is **on by default** for ordinary
  chats. Turn it off if you do not want a new summary saved. Incognito chats
  are never harvested and do not show this switch.
- **Forget what this chat already saved to memory** removes its archived
  transcripts and harvested summaries and facts. Selecting it turns harvest off;
  selecting harvest turns forget off.

Deleting or clearing also removes the chat's session summary and attachments.
Turning harvest off by itself leaves previously saved facts in the knowledge
store; select forget to remove the conversation-derived material too.

The forget option does not remove notes you explicitly asked the agent to
remember, hot memory, background-job reports, or older facts without recorded
chat provenance. Search **Knowledge → Store** and **Memory → Hot memory** for
those and delete them individually. It also does not erase a delegate's copy
of the exchange. See [rooms](/guides/rooms#start-a-fresh-conversation).

## Use incognito {#use-incognito}

For a conversation kept out of automatic memory, right-click a chat tab and
choose **New incognito chat**, or **Shift+click** the tab bar's **+**.
The tab shows an eye-off icon and the composer shows an **incognito** chip.

You can also toggle the current tab with `/incognito` or its right-click menu.
Keep incognito on for the whole conversation: turning it off allows later
turns to summarize earlier content from the same chat. Use a new ordinary
chat when you want to resume normal memory use.

Incognito skips automatic session summaries, conversation harvesting,
compaction archives, and automatic recall of summaries, hot memory, and
documents. Automatic compaction can still shorten the chat; manual `/compact`
refuses because it would need an archive.

The chat still exists in your history, and its messages still go to the model
you chose. Attached files still go to the server; deleting the chat removes its
session attachments. Explicit memory tools, plugins, and delegates have their own reads,
writes, and retention; incognito does not prevent those operations. Use the
delete procedure above when you want to remove the chat itself.

## Share a fact with other agents {#share-a-fact}

Knowledge is private to an agent by default. If your fleet is configured with
a layered commons, **Knowledge → Store** shows `private` and `commons` badges.
Choose **Share** on a private entry to make it available to agents using that
commons; choose **Unshare** to remove the shared copy. The private copy, if
present, remains. See [commons configuration](/reference/knowledge#sharing-knowledge-across-a-fleet-the-commons)
before enabling sharing.

For storage and delivery details, see [Memory and knowledge](/explanation/memory-and-knowledge).
