# forge-orchestration-policy

Deterministic **Adaptive Orchestration** sizing for Forge.

Scores a task or spec's complexity/seniority signals (kind, priority,
blast radius, file/repo counts, requirement + acceptance-criteria counts,
whether it touches contracts/security, dependency count, ambiguity) into:

```python
ComplexitySizing(tier="junior" | "medior" | "senior", strategy="single" | "swarm", score=..., reasons=[...])
```

This is a pure, side-effect-free scoring module. It does not pick a model or
run an agent — later Adaptive Orchestration slices (the model router,
per-role config) consume its `tier`/`strategy` output.

See `forge_orchestration_policy.complexity` for the public API:

* `SizingSignals` — the normalized input signal bundle.
* `signals_from_spec` — builds `SizingSignals` from a `forge_contracts.SpecManifest`
  (plus optional task-level overrides).
* `score_complexity` — the deterministic scoring function.
