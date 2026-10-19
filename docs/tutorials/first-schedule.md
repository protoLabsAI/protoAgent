# Run your first recurring task

Create a small recurring job, observe a result, and delete it. You need a running
agent with a working model connection and scheduling enabled. Keep the agent
running throughout this exercise; a saved schedule cannot run while it is off.

## 1. Create a short test

Open **Work** on the right rail, select the **Schedule** card heading, then
choose **New schedule**.
Select **Cron** and enter `*/2 * * * *` to run every two minutes. Check the
**Timezone** field and the preview before saving.

Use this self-contained prompt:

> Reply with “Schedule check complete.” Do not use tools or change any files.

Set the optional **Job id** to `docs-schedule-check`, then select **Schedule**.
The job should appear in the list. Open it and confirm the prompt, schedule,
timezone, and **Next fire** value. If that ID already exists, use a different
one so you can identify and remove this exercise's job.

## 2. Verify a run

Wait until the next fire time, then open **Feed** from the bottom-left
activity icon. Find the run containing
the test prompt and confirm it completed with a reply. The exact wording can
vary with the model. A saved row or a **Last fire** timestamp alone does not
prove the model answered successfully.

If no result appears, check that the server stayed running, the scheduled time
has passed, and the model still answers a normal chat. Use
[Scheduler behavior](/guides/scheduler#how-firing-works) for missed runs and
[Troubleshooting](/guides/troubleshooting#chat-fails) for model errors.

## 3. Change the cadence

Return to **Schedule**, open `docs-schedule-check`, and select **Edit**.
Choose **Repeat**, select **Daily**, choose a time and named timezone, then
select **Save changes**. Open the row again and confirm the new **Next fire**.

For real work, replace the test prompt with the sources to inspect, what to do,
and where the result should go. A recurring run does not inherit the chat that
created it. Console-created jobs do not get the agent tool's default seven-day
expiry, so delete jobs you no longer need.

## 4. Clean up

Open the test job, select **Delete**, and confirm. Refresh the list and verify
that `docs-schedule-check` is gone. Deleting the schedule prevents future runs;
it does not undo work or cancel a run that already started.

You have now created, observed, edited, and removed a recurring job. Continue
with [Schedule future work](/guides/scheduler) for chat-created schedules,
expiry, and recovery, or [Automate work](/guides/automate-work) to choose between
schedules, watches, goals, and workflows.
