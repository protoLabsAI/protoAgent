# How rooms work

A room is a chat thread containing attributed replies from delegates. There is
no separate room registry: the transcript records who spoke, and the lead
agent reads the same history. For setup and everyday use, see
[Talk to delegates in a room](/guides/rooms).

## Addressing and rounds

The leading run of registered `@name` tokens selects participants. Dispatch is
sequential in that order. An unknown first token returns the roster; an unknown
later token starts the message body. For example, `@coder @unknown review this`
addresses only `coder`, with `@unknown review this` as its message.

Each exchange is stored in the thread with its author. Membership is derived
from participants who have spoken, but does not restrict the lead's
`delegate_to` tool: it can still call any registered delegate.

For a multi-participant address, subsequent rounds keep the original cast and
order. Participants may pass when they have nothing to add. A round with no
replies ends the exchange; otherwise it continues to `room.max_rounds` and
reports an unsettled discussion at the cap. Failed participants leave later
rounds. A single participant, including the only surviving participant, does
not get repeated turns with nothing new to read.

There is no speaker-selection model or global wall-clock deadline. The addressed
run holds the thread's writer lock through dispatch, so additional rounds can
keep that chat busy. Delegate-specific timeouts still apply.

## Catch-up and limits

Every address includes messages since that participant's last reply, derived
from the transcript. On its first address, that range begins at the start of
the thread. It includes operator and lead-agent messages, not just room traffic.
The newest end is retained when either bound is reached:

| Config key | Default | Ceiling |
| --- | --- | --- |
| `room.catchup_max_messages` | 40 | 500 |
| `room.catchup_max_chars` | 8000 | 200000 |
| `room.max_rounds` | 3 | 10 |

Non-positive values fall back to defaults; larger values are clamped at use
time. The YAML stays as written. **Settings → Behavior → Room** exposes the
same bounds and hot-reloads them with **Save & apply**.

A truncation note names only participants whose replies used a clipped view.
Failed dispatches and passes do not earn that note. The catch-up is sent even
when a participant resumes its own conversation, so it remains the minimum
context for stateless peers.

## Participant continuity {#participant-continuity}

Continuity is keyed to the local chat and registered delegate name:

| Delegate type | Retained context |
| --- | --- |
| `acp` | A local coding-agent session for the chat |
| `a2a` | The peer's conversation, when it supplies and honors a `contextId` |
| `openai` | None; each call relies on the supplied messages |

The peer owns its A2A context ID. protoAgent echoes the ID it received rather
than inventing one. A protoAgent peer resolves it to its own chat; another peer
may ignore it, omit it, or reuse one ID across multiple chats. Local thread
isolation cannot guarantee that a foreign peer isolates its conversations.

A synchronous `delegate_to` call and an `@name` address in the same chat share
that participant's conversation. Background delegation, parked-task resume,
and managed-git item claims bypass this helper and use their own contexts.
Registering one peer under two names also gives it two separate local mappings.

The remote mapping is process-local. Restarting, rewinding or deleting the
chat, or changing a delegate's name, URL, or credentials drops it. These actions
do not erase the peer's stored conversation. Rewind and deletion also do not
tear down a local ACP subprocess session. Compaction keeps continuity because
it shortens context without treating past messages as unsaid.

A peer that pauses for input loses the mapping: another address must not queue
behind a question it cannot answer. The lead can resume the parked task using
`delegate_to(target=..., resume_task_id=...)`. A failed address that leaves the
peer working also loses its mapping until a collected reply can restore it;
an unreachable peer retains the mapping because nothing changed remotely.

Incognito does not propagate a memory-retention policy to participants. A
peer stores addressed messages according to its own behavior.

## Slow tasks and late answers

An A2A delegate's `poll_timeout_s` is an inactivity limit. Material changes to
the task's state, context, status message, or artifacts reset it; identical
working-state polls do not. It is not a deadline for the whole task.

When a timeout leaves a known task still working, the room records the failure
and retains its task ID for background collection. The collector issues
read-only `GetTask` requests, backing off to 30-second intervals. It does not
send another message or restart the room's rounds. A completed answer appears
as that member's late message, and the lead gets a turn to consider it. A
failed task is posted as a failure; a paused task hands its question and resume
handle to the lead. Incognito late replies appear without waking the lead.

Collection ends after one hour or eight consecutive failed polls, with a chat
notice. A task the peer no longer knows ends collection quietly. Restart loses
the handle; rewind, deletion, delegate removal, URL changes, and credential
changes withdraw it. Only one task per member per chat is collected: a newer
timeout replaces the older handle.

Foreground `delegate_to` uses the same collection behavior. It requires a peer
that returns a task handle; a peer that holds the request open until completion
may leave no handle to collect. Re-addressing busy peers starts additional work.
