# Read conversations from others

Open the desktop app or browser console for an agent that has received threads
from other people or agents. **From others** lets you read those threads and find
any jobs they started.

1. Select **From others**, pinned first in the chat tab bar. On a phone, open the
   **Chats** sheet and tap **From others** at the top, above your chat rows.
2. Choose a thread under **For you**, or expand **Automated · N**.
3. Read the conversation. Use **← Back** to return to the list; on a phone, use
   **← List** in the app header.

The thread opens in a read-only reader. Your own console chats remain in their
usual tabs.

## What's listed

From others lists A2A threads the agent has with callers outside your console
chats, one row per thread:

- **For you** contains threads whose caller identified itself as a person, or
  identified a person it was acting on behalf of, such as Claude Code for Josh.
- **Automated** contains the other eligible threads, including older threads
  without caller information. Those can read **From another agent**. This group
  starts collapsed unless it is the only group with threads.

Your console chats, background-job threads, command-palette threads, Fleet Room
threads, and the Activity thread are never listed. Neither are deleted threads
or threads opened incognito or hidden. Hiding the **From others** tab is a
separate display preference; it does not make individual threads private.

Rows show a title, origin, time, and a message count when greater than one.
Status badges identify **Needs you**, **Running**, or **Failed**; idle threads
have no badge. The initial list puts threads needing you first within each
group, then running threads, then the rest, newest first within each category.

## The seven-day window and tab count

The initial list covers threads updated in the last **7 days**, rather than
only threads created in that period. The tab's number counts loaded threads
within that window across both groups. It is not an unread-message count.

When **Show older** appears below the list, select it to load an earlier page,
up to 50 threads at a time. Threads outside the seven-day window do not increase
the tab count. The button appears when the previous fetch filled a page; its
absence does not establish that no older thread is stored. The window controls
what the list initially shows, not a promise that transcripts remain stored
indefinitely.

The tab normally appears after loading when there is at least one thread in the
window. A small dot indicates running work; a warning dot takes precedence when
a thread needs you. The phone item shows the same count and status, plus
**Read-only**. Opening a direct conversation link can show the reader even when
the tab would otherwise be hidden or have no recent threads.

## Hide or show From others

On desktop, right-click the tab and choose **Hide “From others”**. On a phone,
long-press its item in the **Chats** sheet to reach the same command. The choice
is saved on that device, not shared as part of your chat list.

To restore it on desktop, right-click empty space in the tab bar and choose
**Show “From others”**. That command is offered while the tab is hidden and has
recent threads to show. A hidden tab comes back when a loaded thread needs you,
provided there are still threads in the seven-day window. A direct conversation
link also opens it without clearing your saved hide preference.

## Read a thread or continue in your own chat

The reader header shows the title, a status badge when applicable, and the origin,
time, and message count when greater than one. It can shorten a repeated caller
prefix in the title; the full title remains in its tooltip. Messages identify
their speakers.

There is no message box. The footer says **Read-only. Replies start a new chat.**
On a phone this is shortened to **Read-only**.

- **Copy link** copies the thread's in-app route, such as
  `#/conversations/example-context`. It is a route within the console, not a
  complete website URL.
- **Continue in a new chat** opens and selects a normal chat with a draft beginning
  `About the conversation "…" (…):`. The draft includes a reference to the original
  thread so the agent can recall it. Edit the draft and send it when ready;
  pressing Continue does not send a message or reply to the original caller.

If the thread is waiting for your answer, the reader shows **This conversation is
waiting for your answer.** Choose **Answer in Needs you** to reach the controls
for answering it. If a transcript has been cleared, the reader instead says
**This conversation is no longer stored.**

## Follow the jobs from a thread

A list row with linked, non-closed jobs ends with **· N open jobs** (or
**· 1 open job**). In the reader header, expand **Jobs from this thread · N** to
see their titles and statuses. The line starts collapsed for each thread and is
absent when there are no open jobs. Here, “open” includes every job that is not
closed, including waiting, in-progress, blocked, and deferred jobs.

If jobs are waiting on you, the line also shows **· N needs you**. Expanded jobs
are ordered **waiting on you**, **in progress**, **blocked**, then **not started**.
Select a job to open its detail in [Work](/guides/react-tauri-ui#work).

In Work, an open job with a recorded source can show **Started in**. For a listed
A2A thread, its title links back to the reader, followed by its origin. If the
thread is not in the loaded list, the link reads **Open the conversation**.
A console-chat source links to that chat; a background-job source reads
**Started by a background job**. A waiting job can show the same source value
under **Source**.

Items in **Needs you** that belong to an outside conversation offer
**Open conversation**. This opens the read-only thread for context, even if
From others was hidden. Use **Answer in Needs you** to return to the answer
controls rather than starting a separate chat to answer the pending request.
