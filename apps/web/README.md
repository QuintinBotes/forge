# @forge/web

The Forge web frontend: a Next.js 15 (app-router) + TypeScript + Tailwind CSS +
shadcn/ui application. Phase-0 establishes the shared substrate every later
frontend task (Board UI, run-trace viewer, etc.) builds on:

- App-router scaffold under `src/app` with a root layout + providers.
- Tailwind CSS + shadcn/ui primitives (`src/components/ui`).
- TanStack Query provider (`src/components/query-provider.tsx`).
- A typed API client stub mirroring the `forge_contracts` DTOs
  (`src/lib/api`) and a small set of TanStack Query hooks.
- The board shell (`src/app/(board)/layout.tsx` + `src/components/app-shell.tsx`).
- A Cmd+K command-palette shell (`src/components/command-palette.tsx`).

## Develop

```bash
pnpm install            # from the repo root (pnpm workspace)
pnpm --filter @forge/web dev
```

Set `NEXT_PUBLIC_API_URL` to point the typed client at the Forge API
(defaults to `http://localhost:8000`).

## Quality gate

```bash
pnpm --filter @forge/web lint     # next lint (eslint-config-next)
pnpm --filter @forge/web build    # next build (also type-checks)
pnpm --filter @forge/web test     # vitest + React Testing Library
```
