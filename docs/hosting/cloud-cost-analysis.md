# Forge Hosting Cost Analysis — BYOK-Hosted Offering

**Audience:** Forge founder, choosing where to run a BYOK (bring-your-own-key) hosted product.
**Prepared:** 2026-07-04.
**Pricing dates:** Fly.io + Tigris pricing pages checked 2026-07-04. Hetzner Cloud reflects the 15-Jun-2026 adjustment; Hetzner Object Storage reflects the 1-Apr-2026 increase. AWS/GCP figures are **reference-grade** list prices (widely published, not re-verified to the same depth this round) and are used only for the compliance-tier comparison.
**Currency:** USD primary. Hetzner is natively EUR ex-VAT (DE/FI); converted at Hetzner's own ~1.18 $/€.

**Two modeled scales (used throughout):**
- **Scale A** — 50 users, ~200 runs/day (~6,000 runs/mo), ~5–8 min avg run, peak ~5–8 concurrent sandboxes.
- **Scale B** — 500 users, ~3,000 runs/day (~90,000 runs/mo), peak ~30–60 concurrent sandboxes, HA on the control plane.

---

## TL;DR (the decision)

- **Run it on Fly.io + Tigris as the primary/single-provider stack.** Per-second billing + scale-to-zero on Firecracker microVMs is a near-perfect fit for bursty 1–15 min agent runs, and Tigris object storage has **zero egress**, which removes the one line item that usually kills egress-sensitive artifact/log/trace stores. Modeled cost: **~$180–220/mo at Scale A, ~$1,900–2,200/mo at Scale B.** [fly.io/docs/about/pricing, tigrisdata.com/pricing]
- **The real dominant cost is not egress — it is Postgres + pgvector** (~40% of the bill at Scale A, ~50% at Scale B). Egress, once you serve artifacts from a zero-egress object store, is a rounding error (~1–2% of the bill). Optimize the database first.
- **Go bare-metal Hetzner only if you have ops muscle and want the lowest steady-state $/core** — and note the architectural catch below: Forge's own Kata+Firecracker sandbox needs `/dev/kvm`, which Hetzner **dedicated (Robot)** gives you and Fly Machines (already microVMs) do **not** nest.
- **Reach for AWS/GCP only when an enterprise buyer's compliance/procurement requirements force it** (BAAs, FedRAMP/HIPAA, data-residency, PrivateLink). You pay a materially higher bill and ~4.5x the egress rate for that trust surface.

---

## 1. Key insight: BYOK inverts the cost structure

For most AI products, LLM inference is the dominant line item. **BYOK / BYOA removes it entirely** — the customer's own key pays the model provider, so Forge's inference cost is **$0**. That structurally re-weights the bill toward raw infrastructure primitives:

1. **Egress** — serving artifacts, traces, logs, and streamed run output.
2. **Bursty compute** — short, isolated, spiky agent runs (1–15 min each).
3. **Postgres + pgvector** — always-on transactional + vector store.

The naive expectation is that **egress** dominates (it is the classic cloud tax). The modeling says otherwise, and this is the decision-grade nuance:

- **Egress is only dominant if you architect it wrong.** Proxying artifacts/logs through the app on a hyperscaler ($0.09/GB AWS) is expensive; serving them from a **zero-egress object store (Tigris)** via presigned URLs makes artifact egress **$0**. With that one move, egress falls to ~1–2% of the bill even at Scale B. [tigrisdata.com/pricing]
- **Bursty compute is cheap if you pick per-second billing + scale-to-zero.** A 5-min run on a Fly performance-1x microVM costs ~$0.0037; a stopped/destroyed Machine costs $0 CPU/RAM. Compute is ~12% (A) to ~20% (B) of the bill. [fly.io/docs/about/pricing]
- **Postgres + pgvector ends up the single largest line item** (~40% at A, ~50% at B) — because the vector index must stay resident in RAM for acceptable latency, and managed-DB tiers jump coarsely.

> **Bottom line:** BYOK shifts the bill from *tokens* to *infra*, and among infra the money goes to the **database**, not egress or compute — **provided** you (a) push all artifact/trace/log egress onto a zero-egress object store and (b) use per-second/scale-to-zero compute for runs. Get those two right and Postgres is your optimization target.

---

## 2. Provider comparison

Estimates are **modeled** with the assumptions in the scale definitions above and are sensitive to run duration, peak concurrency, and Postgres sizing (see §7 uncertainty). Fly/Hetzner figures apply the fact-checker's corrections; AWS/GCP rows are reference-only.

| Provider (compute) | Compute model | Scale-to-zero / per-sec | Egress $/GB (NA/EU) | Managed pgvector | Object storage (egress) | Est. **Scale A** /mo | Est. **Scale B** /mo |
|---|---|---|---|---|---|---|---|
| **Fly.io + Tigris** *(recommended)* | Firecracker microVMs, **per-second** while running | **Yes** — stopped VM = $0 CPU/RAM; ephemeral create-run-destroy = $0 residual | **$0.02** (private cross-region $0.006; APAC/SA $0.04; Africa/India $0.12); ingress free | **Yes** — MPG w/ pgvector, HA + backups + pooling; tiers $38→$72→$282→$962→$1,922 + $0.28/GB storage | **Tigris**, S3-compat, **$0 egress everywhere**; $0.02/GB-mo std | **~$180–220** | **~$1,900–2,200** |
| **Hetzner Cloud** | Shared/dedicated vCPU, **per-hour, rounded up**, capped at monthly | **No** — no serverless, no per-second; always-on worker pool sized to peak | **~$0.0012** (€1/TB); 20–50TB included per server | **No managed DB** — self-host PG16+pgvector on a CCX node + replica + your own HA/PITR | S3-compat, base €6.49/mo incl. 1TB store + 1TB egress; overage egress €1/TB | **~$330** (€280) | **~$2,975** (€2,520) |
| **Hetzner Dedicated (Robot)** | Bare-metal, monthly; **has `/dev/kvm`** | **No** — flat monthly; provisioning mins-to-hours | **~$0.0012** (€1/TB); tens of TB included | Self-host PG16+pgvector (far cheaper per core: EX44 ~€44, AX52 ~€64) | Same Hetzner Object Storage / Storage Box (BX31 10TB €20.80, unlimited free traffic) | **Lower** than Cloud if you self-op (see §3) | **Meaningfully lower** per core; ops cost is the trade |
| **AWS** *(reference only)* | EC2 (no s2z) / Fargate (per-sec, 1-min min) / Lambda (burst) | Partial — Lambda/Fargate yes, EC2 no | **$0.09** (first 10TB, after 100GB free) — ~4.5x Fly | **Yes** — RDS/Aurora PostgreSQL + pgvector; Aurora Serverless v2 scales | **S3** $0.023/GB-mo, **egress $0.09/GB** (not free — the trap) | ~2–3x Fly *(directional)* | ~2–3x Fly *(directional)* |
| **GCP** *(reference only)* | Cloud Run (s2z, per-100ms) / GCE / GKE | **Yes** on Cloud Run | **~$0.12** (varies by dest/tier) | **Yes** — Cloud SQL / AlloyDB + pgvector | **GCS** $0.020/GB-mo, **egress ~$0.12/GB** | ~2–3x Fly *(directional)* | ~2–3x Fly *(directional)* |

**Fact-check corrections applied to the table/estimates:**
- **Hetzner Object Storage overage** — storage overage is **€0.0087/TB-hour (~€6.49/TB-month)** as of the 1-Apr-2026 +30% increase, **not** the pre-April €0.0067/TB-hour (~€4.99). The base price was correctly raised to €6.49 but the source finding inconsistently kept the old per-TB overage; corrected here. Egress overage €1.00/TB is unchanged/correct. [hetzner.com/pressroom/statement-price-adjustment; ubos.tech price-adjustment note]
- **Hetzner Cloud** CPX/CCX rates roughly **doubled on 15-Jun-2026** (CX/CAX +30–40%); estimates use post-adjustment rates, which is why Cloud is no longer the reflexive "cheapest" choice.
- **Fly MPG inter-region transfer became billable (Feb 2026)** — multi-region Postgres now adds transfer cost at Machines rates; single-region estimates assume intra-region (free).

**Why Hetzner Cloud lands ≈ or > Fly here (counterintuitive):** with no scale-to-zero and hour-rounded billing, a per-run VM is uneconomic, so you must run an **always-on worker pool sized to peak concurrency** and pay for idle capacity; and you must run PG+pgvector on **dedicated nodes + a replica** you operate yourself. Fly's per-second bursty compute is dramatically cheaper for this workload, and its managed Postgres removes DB ops. Hetzner only wins decisively on the **bare-metal (Robot)** path, where $/core drops sharply — at the cost of owning HA, backups, Redis, and the DB. [fly.io/docs/about/pricing; hetzner.com]

---

## 3. Recommendation

### 3a. Layered recommendation (best cost/fit per layer)

Treat the three layers independently — they have different demand curves and should not share a pricing model.

**Layer 1 — Agent execution (bursty, isolated, spiky).**
Use **per-second + scale-to-zero microVMs**. Two viable sub-options:
- **(preferred) Fly Machines** — each Machine *is* a Firecracker microVM; per-second billing + built-in auto-stop/auto-start maps 1:1 onto Forge's ephemeral create-run-destroy sandbox lifecycle. Best isolation-per-dollar; you don't run your own Kata runtime. [fly.io/docs/about/pricing]
- **(control-heavy alt) Hetzner Dedicated (Robot)** — bare-metal with `/dev/kvm`, so you can run Forge's own **Kata+Firecracker (`kata-fc`)** with your own guest kernels. Full fidelity to the coded MICROVM isolation tier, but you pay for an always-on pool and own the ops. Choose this only if guest-kernel control or data-locality is a hard requirement.

**Layer 2 — Always-on control plane (API/FastAPI, Next.js web, Celery, Redis, MCP gateway).**
Steady-state → favor **flat/reserved** pricing. On Fly, buy **40%-off annual compute reservation blocks** for the predictable base load (API, web, workers). If you already run Layer 1 on Hetzner bare-metal, co-locate the control plane there on a flat-rate node for the lowest steady-state $/core. Note Fly has **no managed Redis** — self-manage on a Machine or use the Upstash extension.

**Layer 3 — Object storage (artifacts, traces, logs — read-heavy, egress-sensitive).**
**Use Tigris regardless of who runs compute.** It is S3-compatible and has **zero egress across all geographies**, which is the single biggest structural win for this workload. Serve everything via presigned URLs so the trace/log UI costs $0 to read. Tigris also carries an always-free tier (5GB + 10k Class-A + 100k Class-B/mo). [tigrisdata.com/pricing]

**The database (spans layers):** Postgres+pgvector is the dominant line. Managed (Fly MPG) at Scale A for ops simplicity; at Scale B evaluate a **self-managed PG cluster or read-replica strategy** — the coarse managed tier jump ($282 → $962) means you frequently overshoot, and self-managing the vector store is the largest single lever.

### 3b. Single-provider recommendation

**Fly.io + Tigris (both Fly-operated).** Rationale:
- Per-second billing + scale-to-zero is the best-in-class match for bursty, isolated agent runs — you pay only wall-clock seconds.
- Native Firecracker microVM isolation aligns with Forge's per-task sandbox trust model.
- **Tigris zero-egress** removes the #1 cost risk for the artifact/trace/log store.
- Managed Postgres **includes pgvector, HA, backups, pooling** — least ops for a small team.
- One vendor, one bill, one support surface; Anycast Fly Proxy gives free TLS/routing.

**Accepted trade-off:** you cannot nest your own Kata+Firecracker inside a Fly Machine (no `/dev/kvm` inside a microVM), so you map Forge's MICROVM isolation class onto the **Fly Machine boundary** (the platform provides the microVM + guest kernel). This is acceptable because Fly Machines already deliver Firecracker-grade hardware isolation. If that trade is unacceptable, the single-provider alternative is **Hetzner Dedicated** — lowest steady-state cost and full KVM control, at the price of owning HA/backups/DB/Redis and losing scale-to-zero.

---

## 4. Biggest cost levers (ranked)

1. **Serve artifacts/logs/traces from zero-egress object storage (Tigris), never through the app.** Turns egress from a headline risk into ~$0. Single highest-leverage decision.
2. **Optimize Postgres + pgvector first — it is 40–50% of the bill.** Right-size so the vector index fits RAM but no larger; use read replicas for query fan-out; at Scale B, self-manage PG on a flat-rate/bare-metal node to escape the coarse managed-tier jumps ($72 → $282 → $962).
3. **Exploit per-second billing + scale-to-zero for runs.** Ephemeral create-run-destroy = pay only for seconds used. Never run a per-run VM on hour-rounded Hetzner Cloud (a 5-min run bills a full hour → uneconomic per task).
4. **Reserve steady-state control-plane compute** (Fly 40% annual blocks) or move it to flat-rate/bare-metal. Reservations could cut ~$300/mo off the ~$800 always-on+compute at Scale B.
5. **Region discipline.** Fly egress jumps to **$0.12/GB in Africa/India** and $0.04 in APAC/SA; Hetzner Singapore is €7.40/TB. Keep serving in NA/EU, or push egress to Tigris (flat $0 everywhere).
6. **If on Hetzner, bin-pack microVMs onto dedicated nodes** and autoscale nodes coarsely — you pay for peak concurrency, so packing density is the cost driver.

---

## 5. Compliance / enterprise caveat — when AWS/GCP earns its premium

Fly and Hetzner win on cost and workload-fit, but **reach for AWS or GCP when an enterprise deal requires it**, specifically:

- **Signed compliance instruments** — HIPAA BAAs, FedRAMP, PCI-DSS, SOC 2 with a specific auditor, or contractual data-residency the smaller providers can't attest to.
- **Procurement mandates** — a regulated buyer whose security review only accepts a hyperscaler, or existing enterprise cloud commit/credits you'd forfeit.
- **Enterprise networking** — VPC peering, PrivateLink/Private Service Connect, or dedicated interconnect into a customer's environment.
- **Region/certification coverage** Fly/Hetzner lack for a target market.

The premium is real: AWS egress is **$0.09/GB (~4.5x Fly)**, S3 egress is **not free** (removing the Tigris advantage), and managed compute + Aurora/RDS run materially higher — directionally ~2–3x the Fly bill at both scales. That premium buys the **trust and compliance surface** needed to land regulated logos. Pattern to consider: run the BYOK product on Fly+Tigris for the broad market, and stand up an **AWS/GCP deployment only for enterprise/regulated tenants** who fund it. Forge's clean sandbox and vault seams (see §6) make that a deployment-mode switch, not a rewrite.

---

## 6. Migration / architecture note (ties to Forge's existing code)

Forge already has the two seams that make this portable:

- **Firecracker sandbox** — `packages/agent-runtime/forge_agent/sandbox/microvm.py` runs a container inside a Firecracker microVM via **Kata (`kata-fc`)**, with a preflight that **requires the runtime registered *and* `/dev/kvm`** on the daemon host, and a documented isolation ladder (`worktree → container(runc) → gVisor → microvm`) with **no silent downgrade** (`SandboxRuntimeUnavailable` is raised, never a fallback). The isolation classes are frozen in `packages/contracts/forge_contracts/sandbox.py`.
- **Per-tenant vault** — `apps/api/forge_api/auth/vault.py` is an encrypted BYOK secret vault (`SecretCipher`), per-workspace isolated, plaintext never persisted, with a **Postgres-backed store already scaffolded** (the `api_key.encrypted_secret` column in `forge_db`) swappable at the `SecretStore` boundary.

**Mapping onto the recommended stack:**

- **Fly path (`/dev/kvm` catch — call it out):** Fly Machines **are** Firecracker microVMs, and **nested virtualization is not supported** — there is no `/dev/kvm` inside a Machine, so you cannot run your own `kata-fc` there. Map the **MICROVM isolation class to "one Fly Machine per task"**: the platform supplies the microVM + guest kernel, and your `kata-fc` preflight becomes a **deployment-mode assertion** ("platform provides the boundary") rather than a KVM check. You can still run the `container`/`gVisor` tiers *inside* a Machine. Fly's per-second billing + auto-stop/auto-start line up cleanly with the ephemeral create-run-destroy sandbox lifecycle you already implement.
- **Hetzner Dedicated path:** bare metal exposes `/dev/kvm`, so `kata-fc` runs **as coded** with your own guest kernels — full fidelity to the MICROVM tier. Trade: always-on worker pool + self-managed everything.
- **Per-tenant vault:** swap the in-memory `SecretStore` for the Postgres-backed store. Whichever DB you choose (Fly MPG or self-managed PG+pgvector), the vault's `encrypted_secret` column rides along — and the **same pgvector Postgres can host both the vault and the RAG/vector data**, consolidating the dominant cost line. Keep the `SecretCipher` key in the platform secret manager (Fly secrets / your KMS on Hetzner), never in the DB.
- **Object storage:** point the artifact/trace/log writers at **Tigris via an S3 client**, reads through presigned URLs. Zero-egress makes the read-heavy trace UI free to serve.

Net: the recommended stack is a **deployment-mode configuration** over Forge's existing seams, not a re-architecture — which is also what makes the AWS/GCP enterprise-tier fork cheap to add later.

---

## 7. Honest uncertainty & pricing-date caveats

- **Estimates are modeled, not quoted.** They assume ~5–8 min average runs and the stated peak concurrency; they move materially with actual run duration, concurrency, and — most of all — **Postgres sizing** (the coarse managed tiers mean a single tier bump can swing the bill 20–30%). Treat A/B figures as ±20%.
- **Pricing recency:** Fly + Tigris checked 2026-07-04; Hetzner Cloud = post-15-Jun-2026 adjustment; Hetzner Object Storage = post-1-Apr-2026 increase (with the overage correction applied). **AWS/GCP rows are reference list prices, not re-verified this round** — validate before any hyperscaler decision.
- **Known gaps flagged in the findings:** Fly Machine **per-region rate deltas are not cleanly published** (estimates use NA/EU presets); Fly's **operational reputation for occasional networking instability at scale** should be priced as engineering time; **cold-starts** (a few seconds) on scale-to-zero can hurt latency-sensitive first requests. The Hetzner Scale-B source figure was partially truncated and reconstructed from its component line items (~€2,520/mo).
- **Currency risk:** Hetzner is EUR-denominated; USD figures use ~1.18 $/€ and will drift with FX.

---

## Sources (inline citations above)

- Fly.io pricing — https://fly.io/docs/about/pricing/ ; https://fly.io/pricing/ ; https://fly.io/docs/about/billing/ ; https://fly.io/docs/about/cost-management/ ; https://fly.io/calculator/
- Fly Managed Postgres (MPG) — https://fly.io/docs/mpg/
- Tigris object storage — https://www.tigrisdata.com/pricing/
- Hetzner Cloud / Object Storage / Robot — https://www.hetzner.com/ ; price adjustment: https://www.hetzner.com/pressroom/statement-price-adjustment/ ; corroborating overage note: https://ubos.tech/news/hetzner-price-adjustment-updated-cloud-costs-effective-april-2026/
- AWS / GCP — reference list prices (S3/EC2/Fargate/Lambda/RDS; GCS/Cloud Run/Cloud SQL), not re-verified this round; validate at aws.amazon.com/pricing and cloud.google.com/pricing before any decision.
- Forge internal — `packages/agent-runtime/forge_agent/sandbox/microvm.py`, `packages/contracts/forge_contracts/sandbox.py`, `apps/api/forge_api/auth/vault.py`.
