# Forge documentation

> **Forge is pre-1.0 and under active development** — usable for evaluation and
> self-host testing, not yet for production. See
> [`RELEASE_READINESS.md`](../RELEASE_READINESS.md) for the honest per-area
> status.

Start here and follow the path that fits you.

## Learn Forge

| Doc | What it covers |
|-----|----------------|
| **[Getting started](./getting-started.md)** | From a clone to your first orchestrated run in ~15 minutes |
| **[Concepts](./concepts.md)** | The mental model: specs, workflows, agents, runs, knowledge, approvals, policies + a glossary |
| **[Architecture](./architecture.md)** | Services, package map, and how a change flows through the platform |
| **[Trust layer](./trust-layer.md)** | Attested Changesets, Time-Travel Runs, Red-Team Gate, and the Self-Eval Gate — verifiable autonomous change |

## Connect your tools

| Doc | What it covers |
|-----|----------------|
| **[BYOK & bring-your-own board](./integrations/byok-and-boards.md)** | Model-provider keys, reranker, and syncing to Jira / Linear / Asana / Monday / GitHub Projects / ClickUp / Trello / GitLab |
| **[Runbooks](./runbooks)** | Live setup for GitHub App, model providers, reranker, MCP, and Slack |
| **[Examples](../examples/README.md)** | Copy-paste, schema-validated policies, skills, workflows, MCP connectors, and specs |

## Run it yourself

| Doc | What it covers |
|-----|----------------|
| **[Self-hosting quickstart](./self-hosting/quickstart.md)** | Single-host Docker Compose walkthrough |
| **[Docker Compose (production)](./self-hosting/docker-compose.md)** | Hardened, digest-pinned production stack |
| **[Kubernetes / Helm](./self-hosting/kubernetes.md)** | Clustered deployment |
| **[Infrastructure as Code](./self-hosting/iac.md)** | OpenTofu apply for Hetzner + Cloudflare + Fly.io (dev/staging/prod) |
| **[Backup](./self-hosting/backup.md)** · **[Restore](./self-hosting/restore.md)** · **[Upgrade](./self-hosting/upgrade.md)** | Day-2 operations |
| **[Observability](./self-hosting/observability.md)** · **[Reliability](./self-hosting/reliability.md)** · **[Performance](./self-hosting/performance.md)** | Running it well |
| **[Troubleshooting](./self-hosting/troubleshooting.md)** | When something won't come up |

## Security

| Doc | What it covers |
|-----|----------------|
| **[Security policy](../SECURITY.md)** | How to report a vulnerability |
| **[Threat model](./security/threat-model.md)** | STRIDE threat model |
| **[Self-hosting security](./self-hosting/security.md)** | Secret management + hardening |

## Reference & internals

| Doc | What it covers |
|-----|----------------|
| **[Platform specification](./FORGE_SPEC.md)** | The full Forge spec |
| **[Release readiness](../RELEASE_READINESS.md)** | Automated gate status at the beta bar |
| **[Eval results](./EVAL_RESULTS.md)** | Real-corpus retrieval evaluation |

## Contributing

Contributions go through pull requests — see
[CONTRIBUTING.md](../CONTRIBUTING.md) and the
[Code of Conduct](../CODE_OF_CONDUCT.md).
