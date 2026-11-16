# Run your first recurring task

Create a small recurring job, observe a result, and delete it. You need a running
agent with a working model connection and scheduling enabled. Keep the agent
running throughout this exercise; a saved schedule cannot run while it is off.

## 1. Create a short test

Open **Work** on the right rail and select **Coming up**. Choose **New** in the
header, or the **+** beside the **Coming up** heading — both open the same **New**
dialog. (New work always starts here; you never hand-write cron and there is no
Job id field.)

The dialog asks one question — **What should the agent do, and when?** Type the
request in plain words, for example:

> Every hour, reply “Schedule check complete.” Do not use tools or change any files.

Choose **Continue**. The agent reads your words and the dialog becomes
**Check this before saving**, showing its plain reading and where the work will
land — **Goes in Coming up** as a **Time** job. Confirm the fields it read:

- **First run** — set it a few minutes from now so you see a result soon.
- **Repeat** — leave it on **Hourly**. The choices are plain words
  (Once, Every 30 min, Hourly, Daily, Weekdays, Weekly), never a cron expression.
- **Then** — the prompt to run; make sure it is the text above.

Choose **Save**. The job appears in **Coming up**, titled by the first line of
its prompt. Open it and confirm **When** reads *Every hour at :MM* (the minute
you picked), that **Next run** is a few minutes away, and that the prompt is
right. The id, the raw cron string, and the timezone live only under **Technical
details** — the list and detail never show them.

## 2. Verify a run

Wait until the next run time, then open **Feed** from the bottom-left activity
pill. Find the run containing the test prompt and confirm it completed with a
reply. The exact wording can vary with the model. A saved row or a **Last run**
timestamp alone does not prove the model answered successfully.

If no result appears, check that the server stayed running, the run time
has passed, and the model still answers a normal chat. Use
[Scheduler behavior](/guides/scheduler#how-firing-works) for missed runs and
[Troubleshooting](/guides/troubleshooting#chat-fails) for model errors.

## 3. Change the cadence

Return to **Coming up** and open the job (it is titled by the first line of its
prompt — there is no Job id to search by). Choose **Edit**: the same form reopens,
prefilled. Set **Repeat** to **Daily**, pick a **First run** time, then choose
**Save changes**. Open the job again and confirm the new **When** and **Next run**.

For real work, replace the test prompt with the sources to inspect, what to do,
and where the result should go. A recurring run does not inherit the chat that
created it. Console-created jobs do not get the agent tool's default seven-day
expiry, so delete jobs you no longer need.

## 4. Clean up

Open the test job, choose **Delete…**, and confirm. The row disappears and a
toast lets you undo it for a few seconds; after that it is gone. Deleting the
schedule prevents future runs; it does not undo work or cancel a run that already
started.

You have now created, observed, edited, and removed a recurring job. Continue
with [Schedule future work](/guides/scheduler) for chat-created schedules,
expiry, and recovery, or [Automate work](/guides/automate-work) to choose between
schedules, watches, goals, and workflows.
