# deploy/secrets/ — operator-provided secret material (never committed)

This directory holds **env-only** secret files an operator provides at deploy
time. Everything under `deploy/secrets/` is gitignored (see the repo `.gitignore`
`deploy/secrets/` line + the global `*.pem` / `*.key` rules) — no file here is
ever staged or committed. This README is the only tracked file.

## Expected files

| File | Used by | Notes |
|---|---|---|
| `github-app.pem` | HARD-01 GitHub App auth | The GitHub App private key. Read at call time to sign the App JWT; the **value** is never an env var, never logged, never persisted to the DB or an audit row. Mounted read-only into `api` + `worker` at `/run/secrets/github-app.pem`. |

## Placing the GitHub App key

```bash
cp ~/Downloads/your-app.*.private-key.pem deploy/secrets/github-app.pem
chmod 600 deploy/secrets/github-app.pem
```

Then set the matching env (see `.env.integration.example` and
`docs/runbooks/live-github.md`):

```
FORGE_GITHUB_APP_ID=...
FORGE_GITHUB_INSTALLATION_ID=...
FORGE_GITHUB_APP_PRIVATE_KEY_PATH=deploy/secrets/github-app.pem
FORGE_GITHUB_WEBHOOK_SECRET=...
```

When these are unset the integration routes fail **closed** (writes → `501 Not
Configured`; the webhook route stays `401`), never a silent no-auth client.
