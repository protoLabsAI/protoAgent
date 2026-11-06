---
name: paraphrase-test
description: >-
  /paraphrase-test — run a simulated paraphrase test on a text: several reader
  personas each explain it in their own words and say what they'd do next, and
  you report where their understanding differs from what the writer meant. A
  quick proxy for the real-reader test in digital.gov's plain language guide.
user_only: true
slash: paraphrase-test
---

# /paraphrase-test: do readers take away what you meant?

In digital.gov's paraphrase test, 6 to 9 real readers each explain a text in
their own words, and every misunderstanding marks something to fix. This
command simulates it with the `plain_reader` delegate. It is a quick first pass
and **doesn't replace testing with real readers**. Say that in the report.

1. **Get the text and the intent.** Take the text after the command, a file the
   operator names, or your previous reply. Then write down, in two or three
   lines, what the writer means: the main message and the action the reader
   should take. If the operator gave the intent, use theirs. Otherwise state
   yours and label it as your reading.
2. **Choose 3 to 5 personas** who would really read this text. Vary what they
   know: at least one newcomer to the topic, one hurried expert, and one reader
   whose first language isn't English. Describe each in one line, including what
   they already know. Ask the operator for the audience only if you can't tell.
3. **Run the readers.** Delegate to `plain_reader` once per persona, in parallel
   with `task_batch` if it's available, otherwise one `task` call each. Send each
   one the persona line and the full text. Don't send your intent, because the
   readers must not know what the right answer is.
4. **Compare.** For each persona, compare their paraphrase and next action with
   the intent from step 1. Mark each section:
   - **Understood**: they got the message and would take the right action.
   - **Partly**: they got the gist but missed a condition, number or step.
   - **Misread**: they took a different meaning, or would take the wrong action.

   Readers can say a text is clear and still take the wrong action, so weigh the
   NEXT ACTION answer above how confident they sound. Pay attention to a term two
   readers read in different ways. That term needs a definition.
5. **Report.**
   - One line: the personas you used, and a reminder that this is simulated.
   - A table with one row per section and one column per persona, showing
     Understood, Partly or Misread.
   - The problems, worst first. Quote the passage, say what each reader took
     from it, and suggest a fix.
   - Offer to apply the fixes with `plain-language-edit` and run the test again.
     The guide says to test, revise and test again, at least twice.

For text that many people will read (a public page, a form, an announcement),
recommend a real paraphrase test as well: 6 to 9 people, one at a time. Each
reads a section and explains it in their own words, and you record their exact
words without correcting them. Ask what they would do next. Avoid yes-or-no
questions.
