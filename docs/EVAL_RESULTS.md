# Forge Retrieval Eval — Honest Real-Corpus Numbers (HARD-04)

> These are the **real** retrieval-quality numbers: the production hybrid pipeline (semantic pgvector leg + BM25 keyword leg -> RRF k=60 -> cross-encoder rerank) run with a **learned local `sentence-transformers` embedder** over a **real, heterogeneous corpus** (the Forge monorepo). They supersede an earlier **deterministic wiring-check** baseline — an offline embedder + fixture reranker over small golden sets, whose `recall@5 = 1.000` proved only that the pipeline was wired, not its real-world quality.

## Configuration

- **Date:** 2026-07-04
- **Embedder:** local (sentence-transformers/all-MiniLM-L6-v2, 384-dim, no key)
- **Reranker:** fixture (deterministic token-overlap stand-in; learned Jina delta is BYOK/AC9)
- **Corpus files:** 983
- **Corpus commit:** `95228d6`
- **Golden cases:** 36
- **k (recall/MRR):** 5  ·  **nDCG cutoff:** 10

## Headline — full production pipeline (gated)

The complete pipeline: semantic + keyword -> RRF k=60 -> reranker -> top-k. On the no-creds BETA path the reranker is the deterministic **fixture** stand-in (a weak token-overlap placeholder for the creds-gated learned Jina cross-encoder — see the reranker delta below).

| metric | value |
|---|---|
| recall@5 | 0.778 |
| recall@10 | 0.861 |
| MRR | 0.631 |
| nDCG@10 | 0.686 |
| hit_rate@5 | 0.778 |
| cases passed | 28/36 |

**Honesty check:** mean recall@5 = 0.778 is in a realistic band `0 < x < 1` (not perfect by construction). The misses are genuine (a class definition outranked by its tests / consumers / spec docs), exactly the hard cases the eval exists to surface.

## Ablation — fusion adds recall over either leg alone

`hybrid` here is the **RRF-fused** ranking (no reranker), isolating the fusion claim (F05 AC4) from the reranker stage.

| leg | recall@k | MRR | nDCG | hit_rate |
|---|---|---|---|---|
| hybrid (RRF fusion) | 0.847 | 0.695 | 0.750 | 0.861 |
| vector_only | 0.792 | 0.629 | 0.688 | 0.806 |
| keyword_only | 0.806 | 0.529 | 0.611 | 0.806 |

Fused recall - best single leg = **+0.042** (hybrid >= each leg: fusion adds recall).

## Reranker delta (fixture stand-in)

- RRF fusion (no rerank): recall@5 = 0.847
- + **fixture** reranker (BETA stand-in): recall@5 = 0.778  →  **delta -0.069**
- The fixture reranker is a deterministic token-overlap placeholder; on short/identifier queries it can demote good fused results (a negative delta is expected). The **learned Jina/Cohere cross-encoder** delta is the creds-gated measurement (AC9): set `FORGE_EVAL_RERANKER=jina` with a BYOK key in `.env.integration` — PARKED until creds exist.

## Regression gate (committed baseline)

- `FORGE_EVAL_RECALL_FLOOR` = 0.700 (recall@5 floor)
- `FORGE_EVAL_NDCG_FLOOR` = 0.600 (nDCG@10 floor)
- Current run vs floors: **PASS**

## Track 1.4 adversarial refutation — resolution

An earlier concern held that the deterministic eval's 1.000 scores did not prove real retrieval quality — a *realism* gap, not a code defect in `forge_knowledge.sync` or `forge_eval.retrieval_eval`. This real-corpus run with a learned embedder closes that gap. The sync ingestion path is unchanged and remains green; no code defect was surfaced. See the note in `forge_eval/retrieval_eval.py`.

## Appendix — full scorecard

```
Golden retrieval eval — 36 case(s), k=5
------------------------------------------------------------------------
case                      recall@5     MRR   nDCG@10   hit    
real-001                     1.000   0.500     0.631     Y  ok
real-002                     1.000   1.000     1.000     Y  ok
real-003                     1.000   1.000     1.000     Y  ok
real-004                     1.000   0.333     0.500     Y  ok
real-005                     0.000   0.000     0.000     N   X
real-006                     1.000   1.000     1.000     Y  ok
real-007                     1.000   1.000     1.000     Y  ok
real-008                     1.000   1.000     1.000     Y  ok
real-009                     1.000   0.500     0.631     Y  ok
real-010                     1.000   1.000     1.000     Y  ok
real-011                     0.000   0.000     0.000     N   X
real-012                     1.000   1.000     1.000     Y  ok
real-013                     1.000   1.000     1.000     Y  ok
real-014                     1.000   0.500     0.631     Y  ok
real-015                     1.000   1.000     1.000     Y  ok
real-016                     1.000   1.000     1.000     Y  ok
real-017                     0.000   0.000     0.000     N   X
real-018                     1.000   1.000     0.950     Y  ok
real-019                     1.000   1.000     1.000     Y  ok
real-020                     1.000   1.000     1.000     Y  ok
real-021                     0.000   0.000     0.000     N   X
real-022                     1.000   0.250     0.431     Y  ok
real-023                     1.000   1.000     1.000     Y  ok
real-024                     0.000   0.111     0.301     N   X
real-025                     0.000   0.143     0.333     N   X
real-026                     1.000   1.000     1.000     Y  ok
real-027                     1.000   1.000     1.000     Y  ok
real-028                     1.000   0.500     0.631     Y  ok
real-029                     0.000   0.143     0.333     N   X
real-030                     1.000   0.500     0.631     Y  ok
real-031                     1.000   1.000     1.000     Y  ok
real-032                     1.000   0.250     0.431     Y  ok
real-033                     1.000   0.500     0.631     Y  ok
real-034                     1.000   1.000     1.000     Y  ok
real-035                     1.000   0.500     0.631     Y  ok
real-036                     0.000   0.000     0.000     N   X
------------------------------------------------------------------------
mean recall@5=0.778  MRR=0.631  nDCG@10=0.686  hit_rate=0.778  passed=28/36
gate (recall@5 >= 0.700, nDCG@10 >= 0.600): PASS

Ablation — hybrid vs single-leg (same corpus + golden set)
------------------------------------------------------------------------
leg                 recall@k       MRR      nDCG    hit_rate
hybrid                 0.847     0.695     0.750       0.861
vector_only            0.792     0.629     0.688       0.806
keyword_only           0.806     0.529     0.611       0.806
------------------------------------------------------------------------
hybrid recall - best single leg = +0.042 (fusion adds recall: PASS)
```
