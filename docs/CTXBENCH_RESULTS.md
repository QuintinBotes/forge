# ctxbench results

No live run yet.

This file will hold the committed numbers of lore's agent-question benchmark on
forge (spec J §2.4.1): `forge-cli ctxbench report RUN_DIR --write
docs/CTXBENCH_RESULTS.md` replaces it with the headline, by-category,
consistency, per-question and lost-points tables of a graded live run.

The bar: score ≥ 0.92, hallucination ≤ 5.6% and p50 tokens ≤ 17.8k on lore's 30
questions in two consecutive live runs; a CI run fails under the hard floor of
0.86 / 7% / 17.8k.
