# `release/` — release engineering (HARD-12)

Machine-checkable release readiness, versioning, and supply-chain evidence.

| Path | What it is |
|---|---|
| `gates.yaml` | The gate manifest — the machine encoding of `SPEC-PRODUCTION-HARDENING.md`'s ALPHA/BETA/PRODUCTION bars and the 18 lettered gates + the two named human-only asterisks. Consumed by `forge-release-readiness`. |
| `attestations/*.yaml` | Human-only gate sign-offs (`G-PENTEST`, `G-SOAK-FLEET`). Default `signed_off: false`; flip to `true` **only** after the real engagement. The engine never auto-greens these. |
| `scripts/source-sbom.sh` | Generates `sbom/forge-source.cdx.json`, the source-tree CycloneDX SBOM (uv.lock + pnpm-lock.yaml) — distinct from HARD-07's per-image SBOMs under `deploy/sbom/`. |
| `sbom/forge-source.cdx.json` | The generated source SBOM (committed at release time; feeds `G-SEC-EVIDENCE`). |
| `evidence/` | Drop-zone for artifacts other workstreams own (coverage report, parked-closed sign-off). Absent ⇒ the gate honestly reports `MISSING_EVIDENCE`. |

## Common commands

```bash
make release-readiness      # forge-release-readiness --bar production  → RELEASE_READINESS.md (+ exit code)
make source-sbom            # regenerate release/sbom/forge-source.cdx.json (needs syft)
make bump                   # cz bump: next SemVer across every version file + CHANGELOG + tag
make changelog              # regenerate CHANGELOG.md from the commit history
make hooks                  # install the commit-msg conventional-commit guard
```

The engine is honest by construction: a bar is **MET** only when every gate
at-or-below it is `GREEN` or `MANUAL_ATTESTED`. `SKIPPED_NO_CREDS` (a live-cred
gate with no creds), `MISSING_EVIDENCE`, `STALE`, `MANUAL_PENDING`, and `RED` all
mean **NOT MET** — the two human-only gates keep PRODUCTION honestly NOT MET until
a person files a signed attestation.

The tag-triggered signing/provenance/publish half lives in
[`.github/workflows/release.yml`](../.github/workflows/release.yml) and runs only
on a networked CI runner using GitHub-native OIDC + `GITHUB_TOKEN` (no
user-supplied secrets, no stored key material).
