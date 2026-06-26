import type { ReactNode } from "react";

import { AppShell } from "@/components/app-shell";

/**
 * Board route-group shell. Every board view (List, Board, Roadmap, …) renders
 * inside the shared {@link AppShell} chrome. Task 1.6 fills in the views.
 */
export default function BoardLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
