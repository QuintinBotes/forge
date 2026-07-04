# Security Policy

Forge is an open-source engineering-orchestration platform. We take the
security of the platform and of the self-hosters who run it seriously. This
document describes how to report a vulnerability, what to expect from us, and
which versions we support.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security reports.**

Report a suspected vulnerability privately using one of:

- **GitHub Security Advisories** — the preferred channel: open a draft advisory
  at `https://github.com/<org>/forge/security/advisories/new` (Security tab →
  "Report a vulnerability"). This keeps the report private and lets us
  collaborate on a fix in the same place.
- **Email** — `security@forge.example` (replace with your deployment's real
  contact before publishing). Encrypt with our PGP key if the report contains
  sensitive reproduction data.

Please include, where you can:

- a description of the issue and its security impact;
- the affected component (API, worker, MCP gateway, web, a specific package);
- a minimal reproduction (request, config, or test) and the version/commit;
- any suggested remediation.

We support coordinated disclosure. Please give us a reasonable window to fix and
release before any public write-up, and we will credit you (opt-in) in the
advisory.

## Our Commitment / Response SLA

| Stage | Target |
|---|---|
| Acknowledge receipt | within **2 business days** |
| Initial severity triage (CVSS + affected versions) | within **5 business days** |
| Remediation plan shared with the reporter | within **10 business days** |
| Fix released for **critical** issues | as fast as possible, typically **≤ 14 days** |
| Fix released for high/medium/low | with the next scheduled release, prioritised by severity |

We will keep you informed at each stage and coordinate the disclosure timeline
with you.

## Supported Versions

Forge follows [Semantic Versioning](https://semver.org/) (SemVer): the single
source-of-truth version is bumped in lockstep across every package by
`cz bump` (see [`CONTRIBUTING.md`](CONTRIBUTING.md) and the release tooling under
`release/`). Forge is pre-1.0; security fixes land on `main` and in the most
recent tagged `vX.Y.Z` release line.

| Version | Supported |
|---|---|
| `main` (unreleased) | ✅ |
| latest `0.x` release | ✅ |
| older `0.x` releases | ❌ (please upgrade) |

Self-hosters should track the latest release and apply the rotation and upgrade
runbooks in [`docs/self-hosting/security.md`](docs/self-hosting/security.md) and
[`docs/self-hosting/upgrade.md`](docs/self-hosting/upgrade.md).

## What We Enforce (and Continuously Verify)

Security controls are asserted on the **wired request path** by the
enforcement-matrix regression suite (`uv run pytest -m security`) and by a
blocking CI `security` job (SAST + dependency audit + secret scan + SBOM). The
control set and its evidence are documented in:

- [`docs/security/threat-model.md`](docs/security/threat-model.md) — STRIDE
  threat model over the real attack surface.
- [`docs/security/evidence/enforcement-matrix.md`](docs/security/evidence/enforcement-matrix.md)
  — every control and the test that proves it.
- [`docs/security/pentest-punch-list.md`](docs/security/pentest-punch-list.md)
  — scoped hand-off for a third-party penetration test.
- [`SECURITY_FINDINGS.md`](SECURITY_FINDINGS.md) — the automated-scan triage
  (fixed vs. accepted, with reasons).

## Scope

In scope: the Forge API, worker, MCP gateway, web app, and first-party
`forge_*` packages in this repository.

Out of scope: third-party MCP servers, model providers, and integrations you
connect Forge to (report those to their maintainers); social-engineering;
denial-of-service via resource volume against your own self-hosted instance
(tune the documented rate/body limits); and findings that require a
pre-compromised host or physical access.

## A Note on the Human Penetration Test

The automated evidence pack above is **not** a substitute for a third-party
human penetration test or a formal audit sign-off. Those are named, scoped, and
tracked in [`docs/security/pentest-punch-list.md`](docs/security/pentest-punch-list.md)
as the residual external work — the automated gate is the floor, not the
ceiling.
