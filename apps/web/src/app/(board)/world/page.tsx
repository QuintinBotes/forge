import { Suspense } from "react";

import { WorldView } from "@/components/world/world-view";

/**
 * Knowledge graph — the web view over forge's bitemporal world model:
 * search entities, explore a neighbourhood graph, read facts with their
 * evidence on both clocks (world time and known-as-of), inspect conflicts,
 * and accept or reject proposed facts.
 *
 * Wrapped in `Suspense`: `WorldView` reads `?entity=` via
 * `useSearchParams` so a focused entity is linkable, which Next requires a
 * Suspense boundary around for static export.
 */
export default function WorldPage() {
  return (
    <Suspense fallback={null}>
      <WorldView />
    </Suspense>
  );
}
