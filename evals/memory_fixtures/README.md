# PV4 frozen memory fixtures

From a source checkout, install the locked dependencies with `uv sync` and configure
an extraction gateway/model using the normal instance configuration. Regenerate on
an operator workstation with network access and an extraction budget:

```sh
uv run python -m evals.memory_fixtures.build_pv4
uv run python -m evals.memory_fixtures.build_pv4 --users 126 127 --count 40 --model MODEL_ALIAS --yes
```

The builder pages the Hugging Face datasets-server **test** split using stdlib
urllib. Defaults are 12 users sampled from IDs 126–155 with seed 125, and 40
questions. `--seed` controls case selection; the default user cohort stays fixed
unless `--users` is supplied. Cases are balanced round-robin across sorted users,
with seeded shuffling and hard-first ordering within each user. Question, answer,
and all five choices are preserved verbatim, including empty missing choices.

Before extraction it prints the number of chat extraction calls (2,400 for a
fresh default cohort) and requests confirmation. `--yes` bypasses the prompt.
`--model` overrides `routing.aux_model`; otherwise the real extractor uses normal
config auxiliary-model resolution, including its main-model fallback. Each of
200 chats per user is extracted separately, without question/answer leakage, into
an isolated temporary MemoryStore. Sources are `pv4-u{id}-c{index}` (zero-based).
Synthetic dates start at 2000-01-01 and advance one day per chat: these encode
topic order, **not real conversation timestamps**.

Results go to `pv4/cases.jsonl` and `pv4/user_{id}.jsonl`. `--output` changes that
directory. Existing user files are skipped; `--force` rebuilds them. Each user
file is published atomically after all chats finish, so interrupted users can be
retried. Cases are deterministically regenerated after extraction completes.
The runtime extractor is best-effort and logs failures; the builder prints each
user's stored-row count and flags any user that produced no durable facts. Review
those counts, the logs and the fixture contents before accepting a regeneration,
since a failed extraction can otherwise look like a chat with no durable facts.

User JSONL contains every stored row, including superseded audit history, with
IDs, kind, subject, content, delivery, review state, provenance, source session,
supersession link and valid-from date. No embeddings are exported.
`from evals.memory_fixtures import load_fixture_store` provides
`load_fixture_store(path, embed_fn)`: it restores IDs and links into a fresh temp
store, builds FTS and local embeddings, and removes the temp directory when the
store is garbage-collected. Pass `None` for keyword-only retrieval.

## Frozen-fixture policy

This is an on-demand preparation tool, never part of CI, the nightly gate, or
`evals/tasks.json`. Tests use fake fetches and extraction, with real local store
consolidation. Full extraction is intentionally deferred to the fixture-generation
follow-up. Once generated and reviewed, fixtures are frozen evaluation inputs:
regeneration is an explicit, reviewed change. Record the extraction model,
configuration, date, seed, cohort, dataset revision and logs with that change;
model output and the upstream dataset may change even with the same seed.
Never reword a case prompt to name the expected tool, tell the agent to delegate
or avoid tools, or loosen assertions to obtain a pass. Model failures are findings
to report or tier as `tracked`.

## Attribution

PersonalizationV4 is provided by **Memorilla**, described in
[Memorilla (arXiv:2610.21375)](https://arxiv.org/abs/2610.21375), and available at
[memorilla/PersonalizationV4](https://huggingface.co/datasets/memorilla/PersonalizationV4).
Credit also goes to **Synthetic-Persona-Chat**, the underlying synthetic persona
conversation source. PV4 is licensed under
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).
These fixtures adapt that material by selecting questions and extracting durable
facts from conversations; synthetic chronology and supersession metadata are
added by this builder. Retain this attribution when redistributing fixtures.
