import type { ReactNode } from "react";

import { SessionGate } from "@/components/auth/session-gate";
import { AppShell } from "@/components/app-shell";

/**
 * Board route-group shell. Every board view (List, Board, Roadmap, …) renders
 * inside the shared {@link AppShell} chrome, behind a {@link SessionGate}: the
 * API authenticates every route, so without a credential these views can only
 * render skeletons that never resolve. The gate says so instead.
 */
export default function BoardLayout({ children }: { children: ReactNode }) {
  return (
    <AppShell>
      <SessionGate>{children}</SessionGate>
    </AppShell>
  );
}
