# SCF Retrieval Eval — Honest Real-Corpus Numbers (HARD-04)

> These are the **real** retrieval-quality numbers: the production hybrid pipeline (semantic pgvector leg + BM25 keyword leg -> RRF k=60 -> cross-encoder rerank) run with a **learned local `sentence-transformers` embedder** over a **real, heterogeneous corpus** (the SCF monorepo). They supersede an earlier **deterministic wiring-check** baseline — an offline embedder + fixture reranker over small golden sets, whose `recall@5 = 1.000` proved only that the pipeline was wired, not its real-world quality.

## Configuration

- **Date:** 2026-11-17
- **Embedder:** local (sentence-transformers/all-MiniLM-L6-v2)
- **Reranker:** fixture
- **Corpus files:** 1525
- **Corpus commit:** `dd9400f`
- **Golden cases:** 36
- **k (recall/MRR):** 5  ·  **nDCG cutoff:** 10

## Headline — full production pipeline (gated)

The complete pipeline: semantic + keyword -> RRF k=60 -> reranker -> top-k. On the no-creds BETA path the reranker is the deterministic **fixture** stand-in (a weak token-overlap placeholder for the creds-gated learned Jina cross-encoder — see the reranker delta below).

| metric | value |
|---|---|
| recall@5 | 0.889 |
| recall@10 | 0.944 |
| MRR | 0.641 |
| nDCG@10 | 0.715 |
| hit_rate@5 | 0.889 |
| cases passed | 32/36 |

**Honesty check:** mean recall@5 = 0.889 is in a realistic band `0 < x < 1` (not perfect by construction). The misses are genuine (a class definition outranked by its tests / consumers / spec docs), exactly the hard cases the eval exists to surface.

## Ablation — fusion adds recall over either leg alone

`hybrid` here is the **RRF-fused** ranking (no reranker), isolating the fusion claim (F05 AC4) from the reranker stage.

| leg | recall@k | MRR | nDCG | hit_rate |
|---|---|---|---|---|
| hybrid (RRF fusion) | 0.903 | 0.761 | 0.800 | 0.917 |
| vector_only | 0.847 | 0.671 | 0.718 | 0.861 |
| keyword_only | 0.819 | 0.746 | 0.777 | 0.833 |

Fused recall - best single leg = **+0.056** (hybrid >= each leg: fusion adds recall).

## Reranker delta (fixture stand-in)

- RRF fusion (no rerank): recall@5 = 0.903
- + **fixture** reranker (BETA stand-in): recall@5 = 0.889  →  **delta -0.014**
- The fixture reranker is a deterministic token-overlap placeholder; on short/identifier queries it can demote good fused results (a negative delta is expected). The **learned Jina/Cohere cross-encoder** delta is the creds-gated measurement (AC9): set `FORGE_EVAL_RERANKER=jina` with a BYOK key in `.env.integration` — PARKED until creds exist.

## Regression gate (committed baseline)

- `FORGE_EVAL_RECALL_FLOOR` = 0.700 (recall@5 floor)
- `FORGE_EVAL_NDCG_FLOOR` = 0.600 (nDCG@10 floor)
- Current run vs floors: **PASS**

## Track 1.4 adversarial refutation — resolution

An earlier concern held that the deterministic eval's 1.000 scores did not prove real retrieval quality — a *realism* gap, not a code defect in `scf_knowledge.sync` or `scf_eval.retrieval_eval`. This real-corpus run with a learned embedder closes that gap. The sync ingestion path is unchanged and remains green; no code defect was surfaced. See the note in `scf_eval/retrieval_eval.py`.

## Appendix — full scorecard

```
Golden retrieval eval — 36 case(s), k=5
------------------------------------------------------------------------
case                      recall@5     MRR   nDCG@10   hit    
real-001                     1.000   0.333     0.500     Y  ok
real-002                     1.000   1.000     1.000     Y  ok
real-003                     1.000   1.000     1.000     Y  ok
real-004                     1.000   0.500     0.631     Y  ok
real-005                     1.000   1.000     1.000     Y  ok
real-006                     1.000   1.000     1.000     Y  ok
real-007                     1.000   0.500     0.631     Y  ok
real-008                     1.000   1.000     1.000     Y  ok
real-009                     1.000   0.250     0.431     Y  ok
real-010                     1.000   0.500     0.631     Y  ok
real-011                     0.000   0.000     0.000     N   X
real-012                     1.000   1.000     1.000     Y  ok
real-013                     1.000   1.000     1.000     Y  ok
real-014                     1.000   0.333     0.500     Y  ok
real-015                     1.000   0.200     0.387     Y  ok
real-016                     1.000   0.333     0.500     Y  ok
real-017                     0.000   0.167     0.356     N   X
real-018                     1.000   1.000     0.950     Y  ok
real-019                     1.000   0.500     0.631     Y  ok
real-020                     1.000   1.000     1.000     Y  ok
real-021                     0.000   0.125     0.315     N   X
real-022                     1.000   0.500     0.631     Y  ok
real-023                     1.000   1.000     1.000     Y  ok
real-024                     1.000   1.000     1.000     Y  ok
real-025                     1.000   1.000     1.000     Y  ok
real-026                     1.000   0.500     0.631     Y  ok
real-027                     1.000   1.000     1.000     Y  ok
real-028                     1.000   0.500     0.631     Y  ok
real-029                     1.000   0.500     0.631     Y  ok
real-030                     1.000   0.500     0.631     Y  ok
real-031                     1.000   1.000     1.000     Y  ok
real-032                     1.000   1.000     1.000     Y  ok
real-033                     1.000   0.333     0.500     Y  ok
real-034                     1.000   1.000     1.000     Y  ok
real-035                     1.000   0.500     0.631     Y  ok
real-036                     0.000   0.000     0.000     N   X
------------------------------------------------------------------------
mean recall@5=0.889  MRR=0.641  nDCG@10=0.715  hit_rate=0.889  passed=32/36
gate (recall@5 >= 0.700, nDCG@10 >= 0.600): PASS

Ablation — hybrid vs single-leg (same corpus + golden set)
------------------------------------------------------------------------
leg                 recall@k       MRR      nDCG    hit_rate
hybrid                 0.903     0.761     0.800       0.917
vector_only            0.847     0.671     0.718       0.861
keyword_only           0.819     0.746     0.777       0.833
------------------------------------------------------------------------
hybrid recall - best single leg = +0.056 (fusion adds recall: PASS)
```
