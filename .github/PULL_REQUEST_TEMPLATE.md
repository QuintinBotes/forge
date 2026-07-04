<!--
Thanks for contributing to Forge! Please fill out the sections below.
Keep the PR focused — one logical change per PR is easier to review.
-->

## Summary

<!-- What does this PR do, and why? Link the motivation. -->

## Related issue

<!-- e.g. "Closes #123" or "Refs #123". Open an issue first for non-trivial changes. -->
Closes #

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that changes existing behavior)
- [ ] Docs / chore / refactor (no functional change)

## How was this tested?

<!-- Describe the tests you added/ran and how a reviewer can verify. -->

## Checklist

- [ ] I read the contributing guidelines and this change follows the repo conventions.
- [ ] Tests pass locally — Python: `make test` (`uv run pytest`); Web: `pnpm --filter @forge/web test` (if `apps/web` was touched).
- [ ] Lint & types are clean — Python: `uv run ruff check .` + `make typecheck`; Web: `pnpm -r lint` + `pnpm --filter @forge/web build` (if `apps/web` was touched).
- [ ] I added or updated tests covering my change (TDD: failing test first).
- [ ] I updated the relevant docs (`README.md`, `docs/`, `.env.example`) where behavior or config changed.
- [ ] No secrets, credentials, or real customer data are included (a gitleaks gate runs in CI).
- [ ] This PR is scoped to a single logical change and the description explains it.
- [ ] For frozen `packages/contracts` changes: this is an intentional contract change and downstream consumers were updated.
