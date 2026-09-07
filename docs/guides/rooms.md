# Rooms — `@name` group chat in a thread

Type **`@proto what broke the build?`** in chat and `proto` answers *in the transcript,
under its own name*. No lead-agent turn runs, nothing is paraphrased, and the answer is
the delegate's own words. Address several at once — `@proto @reviewer what broke?` —
and you have a **room**: a conversation with more than two participants in it.

A room is not a separate feature with its own store, panel, or lifecycle. It is what a
chat thread *is* once more than one voice has spoken in it.

## What `@name` does

1. The leading run of `@` tokens is resolved against your [delegate
   roster](/guides/delegates). `@proto @reviewer fix it` addresses **both**; the first
   token that doesn't resolve begins the message, so `@proto @nope hi` addresses `proto`
   with the message `@nope hi` — a mid-message `@` is prose, as always.
2. Each addressee is dispatched **sequentially, in the order you wrote them**, with a
   catch-up on the room (below).
3. Both halves of each exchange — your message and the reply — are written onto the
   thread, attributed.

If the *first* token names no reachable delegate you get the roster back instead of the
text being run as a prompt:

```
Unknown or unreachable delegate: @prto. Available: proto, reviewer, claude-code.
```

A delegate that fails to answer is recorded as having failed, in the room, rather than
vanishing:

```
Delegate @proto failed: connection refused
```

That matters: the *next* thing you type goes to the **lead agent**, and it needs to know
the address happened.

## The thread is the transcript

There is no room table, no room registry, no participant list on disk. The
checkpointer thread that already holds your chat holds the room too — each room message
is stamped structurally with who said it (`<room-message from="proto">`).

Everything else is derived from those stamps, which is the point:

- **Membership.** "Who is in this chat" is *whoever has spoken here*. The lead agent is
  told, at each model call, who the cast is — recomputed from the thread every time, so
  it can never drift from what actually happened.
- **Catch-up.** "The room since you last spoke" is computed from the same stamps rather
  than from a stored watermark. A stored watermark can drift; a derived one cannot.

And because the room is the thread, the lead agent reads all of it. Ask *"so what did
they decide?"* one message later and it answers from the same history you're looking at.

::: tip Membership is awareness, not permission
Naming the cast makes the lead *prefer* participants who already have the context — the
same reason you would. It never fences `delegate_to`: every delegate on the roster stays
reachable whether or not it has spoken here.
:::

## Catch-up: the room since you last spoke

An addressed participant is not handed the whole conversation. It gets the messages that
landed **since its own last message**, attributed by author:

```
You are taking part in a group chat. Here is what has been said since you last spoke:

[operator] what broke the build?
[reviewer] I'd blame auth

You have been addressed directly as @proto. Reply to this message:

what broke the build?
```

That window is what keeps the cost of a room proportional to *the conversation* rather
than to its length. It is also, for most delegate types, the **only** continuity there
is — see [the limitation](#the-conversation-key-limitation) below.

The window is bounded twice, and whichever bound trips first wins:

| Knob | Default | Max | Bounds |
|---|---|---|---|
| `room.catchup_max_messages` | `40` | `500` | how many **thread messages** are replayed |
| `room.catchup_max_chars` | `8000` | `200000` | the total size of that replay |

"Thread messages", not "room messages": the window counts every authored message since
the participant last spoke — your own turns and the lead agent's replies as well as room
traffic. So a first `@` in a long ordinary chat can truncate before a single room message
exists, and `40` is 40 *messages of any kind*, not 40 replies from participants.

The window is taken from the **newest end** — the messages being replied to. A
participant that has been quiet for 300 messages gets the recent room, not a
context-window-sized bill for its own silence.

**When it truncates, you are told.** The reply carries a note naming each participant
whose answer was given on a clipped view, and which knob to raise:

> _Older messages were left out of the catch-up for @proto — the room since they last
> spoke is longer than the window. Raise `room.catchup_max_messages` /
> `room.catchup_max_chars` to widen it._

This exists because the alternative — dropping the tail quietly — leaves you with a
confident answer given on a partial view of the room and no way to know. The workaround
people reach for is to re-mention, which fragments the very conversation the room is
holding.

Only participants that actually **answered** are named. A dispatch that failed never
produced an answer for the clipping to have shaped, and a note under `Delegate @proto
failed: connection refused` telling you to widen a catch-up window would point you at a
knob that has nothing to do with the failure. A `pass` is excluded for the same reason.

::: tip On a first address, "since they last spoke" means the whole thread
A participant that has never spoken here has no watermark, so its window starts at the
top of the thread — mostly your chat with the lead agent. That is the common way this
note appears on the very first `@` in a busy thread.
:::

## Multi-round rooms

By default an address is **one round**: each addressee answers once, and the exchange
ends. That is fine for *"ask two people the same thing"* and useless for *"let them work
it out"* — neither ever sees the other's answer as something to respond to.

Raise `room.max_rounds` and the same cast runs again:

```yaml
room:
  max_rounds: 3
```

- **The cast is the addressed set**, resolved before anything is dispatched, and it never
  grows. Rounds 2..N re-run exactly those participants, in the order you wrote them — so
  each one now sees, through its catch-up, what the others just said. Nobody is ever
  dispatched only to decide they had nothing to say.
- **Participants are *told* they can pass.** Above 1 round, the prompt an addressee
  receives ends with the offer: *"If you have nothing to add, reply with exactly `pass`
  and nothing else."* That instruction is the whole mechanism — a model that was never
  invited to decline does not decline, and every room would then run to its cap. At
  `max_rounds: 1` the offer is not sent at all, which is why single-round prompts are
  unchanged.
- **`pass` means silence.** A reply that is empty, or is just `pass` — bare, in parens,
  with a full stop, in backticks, quoted or emphasised — is not written to the room and
  does not count as speaking. A participant with nothing to add can say so without either
  polluting the transcript or keeping the room alive. (`pass` *inside* a sentence — "I'd
  pass on that approach" — is an answer, and so is a qualified one: *"Pass, but note the
  auth change"* carries something the room needs, so it is recorded and it keeps the room
  going.)
- **A round in which nobody spoke settles the room.** It stops there. This is the normal,
  good ending, and it is deliberately quiet: no note, nothing added to the reply.
- **A participant with nothing new to read is told so.** If everyone who spoke after it
  was silent, its catch-up is empty; instead of re-sending the original question as if
  nothing had happened — which gets you the same answer twice, word for word — the prompt
  says *"Nothing new has been said since you last spoke"*, and it can pass.
- **The cap is the backstop, and it announces itself.** If `room.max_rounds` rounds run
  and the conversation still hasn't settled, the reply says so:

  > _The room stopped at its 3-round cap — the conversation had not settled. Ask again
  > to continue it, or raise `room.max_rounds`._

- **A failed address is not retried.** A participant whose dispatch failed is dropped
  from later rounds. A dead delegate does not answer faster the third time, and retrying
  it once per round is how a bounded room turns into N times the timeout you wait
  through. That holds even for the failure that says the peer is *alive* — `still
  running after 300s — the peer may still be working`: a room passes no resume handle, so
  a retry opens a **second** task on a peer already busy with the first, waits the same
  timeout again, and still returns nothing. The member is not quietly written off: its
  failure is recorded in the room, and the adapter's own wording (including "raise its
  poll timeout") is quoted straight to you.
- **A room needs two participants to be a room.** `@one-agent do X` is one round however
  high `max_rounds` is, and so is a room that has lost all but one participant. With a
  single speaker there is nothing new between its own reply and its next turn — the
  catch-up is empty by construction — so a further round would re-send your original words
  verbatim to a delegate that already carried them out, file writes and bill included.
  `room.max_rounds` is one global knob; it must not multiply the cost of the most common
  address, which is to a single agent.

At `max_rounds: 1` — the default — almost none of this is observable: no pass offer in
the prompt, no pass handling, no cap note, exactly the single pass rooms have always
made. The one addition on the default path is the **truncation note** above, and only
when the window actually truncated.

### When to raise it

Raise it when you want the participants to **react to each other**, not just answer you:
a design disagreement between two agents, a review where one raises something the other
should respond to, a plan two specialists have to reconcile. One round cannot do any of
those — the second addressee sees the first's answer, but the first never sees a word
back.

Leave it at 1 when you are fanning the *same* question out to several participants and
intend to read the answers yourself. A second round there costs dispatches and buys you
agreement noise.

The cost is easy to reason about, which is the point of bounding rounds rather than time:
**at most `max_rounds` × (participants) dispatches**, minus everyone who passes, and the
room stops the moment a whole round is silent. Budget for the cap, not for a settle: a
round only goes quiet if *every* participant emits a bare `pass`, so how often a room
settles early is a property of your models, not a guarantee. Start at `2` or `3`. If you
never see the cap note, your rooms are ending by settling — the healthy case, with the cap
doing its job as a backstop; if you see it every time, the conversation needs more room
than you gave it (or the participants are talking past each other, which more rounds
won't fix).

Every round is also written **permanently onto the thread**, because the thread is the
transcript — so N rounds of participant output ride in every later lead-agent turn's
context until compaction. That is the second half of the cost, and it is why the cap is
low by default.

::: warning Rounds are bounded; time is not
There is deliberately **no wall-clock cap** on a round. A turn-length limit declares work
dead while a participant is still doing it, and a room that "settles" around members who
were mid-deploy is worse than one that takes a while. Bound the number of rounds instead.

The flip side: the whole room runs inside the thread's write lock, so with a high
`max_rounds` and slow participants every other writer on that thread — a scheduled fire, a
goal continuation, an inbound A2A turn, compact — waits for it. Each dispatch is still
bounded by the delegate's own configured timeout (`poll_timeout_s` for `a2a`, `timeout_s`
for `acp`); the room adds none of its own.
:::

### Who decides who speaks next

Only **you** (by addressing) and the **lead agent** (by calling `delegate_to`). The round
driver picks the next speakers from the addressed set and the room's own structure — it
never reads a delegate's reply for intent. A delegate cannot pull a third party into the
room by mentioning them; reply-text chaining shipped once and was removed as a capability
leak. The one thing read out of a reply is whether it was a `pass`.

## Configuration

All three knobs live in `langgraph-config.yaml` and are editable from **Settings ▸
Behavior ▸ Room**. All are hot-reloaded on Save & Reload.

```yaml
room:
  catchup_max_messages: 40   # messages replayed to an addressed participant
  catchup_max_chars: 8000    # …and the size ceiling on that replay
  max_rounds: 1              # 1 = each addressee answers once (the default)
```

A zero or negative value on any of them is read as "leave it at the default", never as
"send nothing" or "address nobody". Each also has a ceiling — `500` messages, `200000`
characters, `10` rounds — and a larger value is clamped to it rather than obeyed. These
are per-dispatch cost knobs: an unbounded catch-up ships the whole thread as the prompt
on every address, and because an addressed run holds the thread lock from start to
finish, an unbounded round count parks your own thread behind every one of those
dispatches. The Settings form will not offer a value above the ceiling; a hand-edited
`langgraph-config.yaml` is kept as written and clamped when the room reads it.

## The `conversation_key` limitation

Be honest about what a participant remembers between addresses: **usually nothing.**

`conversation_key` — the parameter that gives a delegate a persistent session of its own —
is **ACP-only**. `DelegateRegistry.dispatch` refuses it for every other type. So:

| Delegate type | Between addresses it remembers… |
|---|---|
| **acp** (protoCLI, Claude Code, …) | its own session, keyed to this thread |
| **a2a** (a fleet agent) | nothing |
| **openai** (a model endpoint) | nothing |

For everything but `acp`, **the catch-up window is the participant's entire picture of
the room.** That is why the caps are worth tuning, why truncation is surfaced rather than
swallowed, and why a room of `a2a` members costs more prompt per round than a room of ACP
coding agents.

## See also

- [Delegates](/guides/delegates) — the roster `@name` resolves against
- [CLI coding agents over ACP](/guides/coding-agents) — the one type with its own session
- [Fleet](/guides/fleet) — many named agents on one host, addressable over `a2a`
