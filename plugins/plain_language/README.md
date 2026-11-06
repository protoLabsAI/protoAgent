# Plain Language

Helps the agent write so that the reader understands it the first time they
read it. The rules come from the
[digital.gov plain language guide](https://digital.gov/guides/plain-language),
which is adapted from the federal PlainLanguage.gov guidelines.

Plain language means clear for the reader, not simplified. The agent keeps the
technical terms its reader uses, and explains the ones they might not know.

## What it adds

| Part | What it does | When it applies |
|---|---|---|
| Core rules | A short rule list added to the system message on every model call: bottom line first, active voice, verbs instead of nouns, common words, short paragraphs, one term per idea, and descriptive link text | Every reply, while **Apply the core plain-language rules to every reply** is on |
| `plain-language-write` skill | The full checklist: audience, order, sentences, words and layout | The agent loads it for long drafts such as docs, emails, reports and release notes |
| `plain-language-edit` skill | Rewrites a text in plain language, keeps every fact, and reports what changed | When you ask it to rewrite, simplify or clarify something |
| `/plain-review` | Reviews a text and lists findings by severity, each with a suggested fix | When you type it |
| `/paraphrase-test` | Simulated readers explain the text in their own words and say what they would do next. The agent then reports where they misread it | When you type it |
| `plain_language_check` tool | Checks the mechanical rules the same way every time: filler, complex words, "and/or", "e.g.", "shall", vague link text, paragraph and sentence length, heading depth, lists without a lead-in, undefined abbreviations | The agent runs it on long drafts. The two commands above also use it |
| `plain_reader` subagent | Plays one reader persona for `/paraphrase-test` | Used by `/paraphrase-test` |

The plugin has no network or filesystem access. The core rules never change, so
they're cached along with the rest of the system prompt.

If you also use the Cowork writing-voice profile, the profile sets tone and
personality and these rules cover clarity.

## Turn it off

The plugin is on by default. To turn it off, open **Settings ▸ Plugins**, or add
it to your config:

```yaml
plugins:
  disabled: [plain_language]
```

To keep the skills and commands but drop the rules from every reply, turn off
**Apply the core plain-language rules to every reply** in the plugin's settings.

## Measure it

`eval/ab.py` starts two throwaway agents from the bundled template, one with the
plugin and one without, and sends both the writing tasks in `eval/prompts.json`.
It scores the replies three ways:

- **Blind pairwise judge.** A judge model compares the two replies. It doesn't
  know which agent wrote which, and it isn't told about plain language. It picks
  the reply that the stated reader would understand faster and more accurately,
  without losing anything they need. Each pair is judged twice, once in each
  order.
- **Facts kept.** Checks that each reply includes the facts the task requires.
- **Lint score.** Findings per 100 words from the `plain_language_check` tool,
  plus reply length.

To run it, from the repo root:

```bash
python plugins/plain_language/eval/ab.py --repeat 3 --judge-model anthropic-oauth:claude-opus-5-5
```

The report is saved to `evals/results/plain-language-ab-<ts>.md`. The script
removes any inherited `PROTOAGENT_*` variables first. That way, running it from
inside a desktop-hosted agent can't touch that agent's data.

Loader tests (`tests/test_plain_language_plugin.py`) check the packaging and
the linter. Whether replies actually get clearer is a question about the model,
and the A/B answers it.

## Credits

The rules, the numeric limits and the paraphrase-test method are adapted from
digital.gov's [plain language guide](https://digital.gov/guides/plain-language):
[principles](https://digital.gov/guides/plain-language/principles),
[writing](https://digital.gov/guides/plain-language/writing),
[design](https://digital.gov/guides/plain-language/design) and
[testing](https://digital.gov/guides/plain-language/test). The guide is a US
federal government work, adapted from PlainLanguage.gov. The guide gives no
sentence-length limit, so the checker's limits (over 30 words is minor, over 40
is major) are this plugin's own.
