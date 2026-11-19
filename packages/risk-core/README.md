# scf-risk

Risk classification for Swarm Code Factory (spec A, `specs/004-risk-classification`).
It derives a risk class for a change from facts about its diff and the repository
policy's `risk_rules` block, then checks a declared class against that derived floor.

Pure and I/O-free: no database, network, filesystem or subprocess. The models
(`RiskClass`, `RiskRules`, `ChangeSignals`, `DerivedRisk`, `RiskEvaluation`,
`RiskOverride`, `GateRequirements`) live in `scf_contracts`; this package computes.

```yaml
risk_rules:
  enforcement: shadow          # shadow | enforcing (default shadow)
  default: low                 # required; the floor for every change
  fallback: critical           # used when the diff cannot be read (default critical)
  derive:
    - paths: ["app/auth/**"]
      class: high
    - id: billing
      paths: ["app/billing/**"]
      class: critical
  classes:
    high:     { approvals: 2, deploy: gated, red_team: false, self_eval: true }
    critical: { approvals: 2, deploy: gated, red_team: true,  self_eval: true }
```

- `scf_risk.signals.signals_from_changes(changes)`: the `ChangeSignals` of a diff from
  its `(path, lines added, lines removed)` entries. A migration or a contract or schema
  touch is read from the paths alone (a `migrations`/`alembic` directory; a
  `contracts`/`schemas`/`proto` directory, a `.proto`/`.graphql`/`.avsc` file or an
  `openapi.*` file).
- `scf_risk.classifier.derive(signals, rules)`: `max(default, class of every matching
  rule)`, with the ids of the matching rules in policy order; `fallback` when the
  signals are marked unavailable. It never raises.
- `scf_risk.evaluation.evaluate(derived, declared, override)`: a declaration at or
  above the floor wins; one below it raises `RiskDeEscalationError` unless a
  `RiskOverride` names an approver and a reason, which the result then records.
- `scf_risk.requirements.requirements_for(risk_class, rules)`: the gate requirements
  the policy configures for a class. A class it does not configure takes those of the
  highest configured class below it, and adds none when there is no such class.

Rule paths use the same glob matcher as the `matches_glob` policy condition. Write
them relative to the repository root, with no leading `/` or `./`, and match a
directory with `<dir>/**`. Invalid rules, including duplicate rule ids, are rejected
when the policy loads, by `scf_policy.load_policy`.
