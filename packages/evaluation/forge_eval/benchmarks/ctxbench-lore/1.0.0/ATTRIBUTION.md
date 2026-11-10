# Attribution

This suite is derived from **the lore project (Apache-2.0)**: its context-store
agent benchmark article, `docs/building-lore/context-store-agent-questions.md`
("Lore or Utopia: what an agent can answer with each", runs of 2026-10-27), in
the repository `https://github.com/{org}/lore`. The organisation is a runtime
value (`FORGE_CTXBENCH_SUBJECT`) and is not written here.

## Copied from lore

- **Questions** (`questions.json`): the 30 questions about lore itself, verbatim,
  with their ids, kinds and evidence column.
- **Gold answers** (`questions.json`): all 30 verbatim, copied from the article's
  raw markdown (fetched 2026-11-08 from `main`; its lore rows are identical at
  the article's first commit). The only edits: the organisation name is
  replaced by `{org}`, and the markdown table escape `\|` is written as `|`.
- **Published verdicts and aggregates** (`reference.json`): the per-ask verdicts of
  the no-MCP, Lore and first Utopia runs, the per-question scores of the two
  later Utopia runs, and the article's 60-question headline, by-kind,
  consistency and head-to-head figures.

## Changed (Apache-2.0 §4(b))

- Only lore's own 30 questions are included. The article's other 30 concern a
  private repository and are omitted.
- The organisation name is templated as `{org}`.
- The bank is JSON, not YAML, and adds a `corpus` field per question (forge's own
  classification of the sources an answer needs) and a `gold_verbatim` flag.

## New in forge (not from lore)

- The prompts (`prompts/agent.md`, `prompts/judge.md`) and the rubric wording:
  lore's were never published, so these are comparable in shape only.
- The harness (`forge_eval.ctxbench`, `forge-cli ctxbench`), including the
  scoring code, which implements the method the article describes.

## Licence

lore is licensed under the Apache License 2.0, as is forge (the repository's
root `LICENSE`). lore has no `NOTICE` file (checked 2026-11-08), so there is no
notice to carry.
