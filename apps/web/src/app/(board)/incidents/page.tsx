import { IncidentsView } from "@/components/incidents/incidents-view";

/**
 * Incidents (F17) — the incident command center. A severity-ranked queue beside
 * the selected incident's lifecycle: declare dialog, lifecycle + blast-radius
 * badges, the FSM action bar, the response timeline, the remediation runbook
 * panel and the postmortem view with its action items. Backed by the typed
 * `/incidents` router with keyboard-first control (`j/k` move, `c` declares).
 */
export default function IncidentsPage() {
  return <IncidentsView />;
}
