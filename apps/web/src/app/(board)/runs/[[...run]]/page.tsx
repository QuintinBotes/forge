import { RunTraceViewer } from "@/components/run-trace/run-trace-viewer";

/**
 * Run-trace viewer route. `/runs` shows the run-id entry form; `/runs/{id}`
 * opens that run's step-level trace. Modelled as an optional catch-all so a
 * single screen serves both without a redirect.
 */
export default async function RunTracePage({
  params,
}: {
  params: Promise<{ run?: string[] }>;
}) {
  const { run } = await params;
  const runId = run?.[0];
  return <RunTraceViewer runId={runId} />;
}
