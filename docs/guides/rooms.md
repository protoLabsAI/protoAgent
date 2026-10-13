# Talk to delegates in a room

Start a chat message with `@name` to get a delegate's own reply in your chat.
Address several delegates to let them discuss the same task. The conversation
stays in the current chat; you don't need to create a separate room.

## Set up and send a message {#what-name-does}

1. Add the participants in **Settings → Delegates** and test their connections.
   Use each delegate's registered name. See [Delegates](/guides/delegates) if you
   haven't connected one yet.
2. Start your message with the names, followed by the task:

   ```text
   @coder @reviewer check the login change. Identify failures before editing files.
   ```

3. Read the replies under each participant's name. They answer in the order you
   named them; later participants can see earlier replies.
4. Send a message without a leading `@name` to return to the lead agent:

   ```text
   Summarize their findings and propose the next step.
   ```

One name, such as `@reviewer check this plan`, requests one reply. An `@name`
inside ordinary message text does not direct the message to that participant.
If the first name is unknown, the chat shows the available delegates. Check the
spelling against that list.

## Continue the discussion {#multi-round-rooms}

With several addressees, the same participants can reply for up to **three
rounds** by default. The exchange ends earlier if everyone passes. A failed
participant drops out of later rounds. A single addressee gets one turn.

When you see **“The room stopped at its 3-round cap”**, the participants had not
finished discussing. Read their proposals before continuing. To continue,
address the participants again with the remaining question:

```text
@coder @reviewer agree on the smallest fix and the checks needed to verify it.
```

<span id="when-to-raise-it"></span>
<span id="who-decides-who-speaks-next"></span>

For a longer discussion, raise **Max rounds per address** in **Settings → Behavior → Room**
and use **Save & apply**. Set it to `1` when you want each participant to answer
once. Extra rounds can increase waiting time and model usage; a round cap does
not mean the participants reached agreement.

## Supply enough context {#catch-up-the-room-since-you-last-spoke}

Participants receive recent messages since they last spoke, bounded by **40
messages** and **8,000 characters** by default. A participant joining for the
first time receives the recent end of the current chat. Your messages and the
lead agent's replies count toward these limits too.

A note naming a participant says when older messages were left out of its
catch-up. Include the missing decision, relevant file paths, or constraints in
your next request. If this happens repeatedly, raise the catch-up limits in
**Settings → Behavior → Room**.

<span id="what-that-changes-about-tuning-the-caps"></span>

Don't assume every participant can recover omitted history: coding agents
and some remote agents keep their own conversations, while model endpoints
rely on the supplied catch-up. See [participant continuity](/explanation/rooms#participant-continuity)
for the differences.

## Recover from a failed or slow reply {#late-answers-from-a-member-still-working}

| What you see | What to do |
| --- | --- |
| Unknown or unreachable name | Check the delegate roster, spelling, and connection test |
| Connection refused or authentication error | Check the delegate's URL, running state, and credentials in **Settings → Delegates**, then test again |
| “Still running … without observable progress” | The remote agent may still be working. Avoid sending the same task again while it is busy |
| “Still waiting … in the background” | A late answer will be posted in this chat if the remote task finishes; it does not restart the room's rounds |
| A participant needs input | Give the lead agent the requested answer and the displayed resume handle so it can resume the delegated task |

<span id="what-continuity-is-not"></span>

Re-addressing an agent that is still working starts another task. It does not
join the existing one. Collection gives up after an hour or repeated failed polls, with a chat notice.
A restart stops collection because the pending handle is not persisted.
You can inspect the remote agent's own console to check work that continues there.

## Start a fresh conversation {#what-a-participant-remembers-between-addresses}

<span id="start-a-fresh-conversation"></span>
<span id="the-conversation-is-the-chat-not-the"></span>
<span id="what-continuity-does-not-survive"></span>

Use a new chat when you want a separate task. In the same chat, subsequent
addresses can continue a participant's own conversation. Restarting clears
local pointers used to resume remote conversations. Rewinding or deleting
a chat clears those remote pointers too, but does not erase the remote agent's
copy. A local coding agent's live session can retain its history after a rewind.

Incognito controls the lead agent's memory handling. It does not make a
delegate's conversation incognito or erase content the delegate stores. See
[Manage memory](/guides/manage-memory#use-incognito).

## Room settings {#configuration}

Change these in **Settings → Behavior → Room**, then **Save & apply**:

| Setting | Default | Maximum |
| --- | --- | --- |
| Catch-up window (messages) | 40 | 500 |
| Catch-up window (characters) | 8,000 | 200,000 |
| Max rounds per address | 3 | 10 |

Zero or negative values restore the default; they do not turn rooms off.
The limits apply to context and rounds, not to the total elapsed time of a task.

## Further reading {#see-also}

<span id="the-thread-is-the-transcript"></span>

- [How rooms work](/explanation/rooms): transcript, dispatch, continuity, and late answers.
- [Delegates](/guides/delegates): connect and test participants.
- [CLI coding agents](/guides/coding-agents): connect a coding agent.
- [Fleet](/guides/fleet): run several agents on one host.
