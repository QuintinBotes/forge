import { SpecDashboard } from "@/components/spec/spec-dashboard";

/**
 * Spec validation — the SDD lifecycle dashboard. Traces every spec from draft
 * through validation: the forge lifecycle rail, validation gates, and the
 * requirement->task->test traceability matrix, with the manifest and project
 * constitution alongside. Backed by the typed F02 `/spec` engine (F23).
 */
export default function SpecsPage() {
  return <SpecDashboard />;
}
