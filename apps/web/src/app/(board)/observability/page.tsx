import { ObservabilityView } from "@/components/observability/observability-view";

/**
 * Observability & cost (F38) — the workspace telemetry dashboard: token/cost per
 * phase, provider and model, spend over time, retrieval quality (recall@k,
 * reranker uplift, index freshness) and per-stage retrieval latency. Backed by
 * the typed `/cost` rollups and the `/observability/metrics` scrape.
 */
export default function ObservabilityPage() {
  return <ObservabilityView />;
}
