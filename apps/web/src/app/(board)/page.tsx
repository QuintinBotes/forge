import { LayoutList } from "lucide-react";

/**
 * Board home (List view) — Phase-0 empty-state placeholder. Task 1.6 replaces
 * this with the real list/kanban views backed by the typed API client.
 */
export default function BoardHomePage() {
  return (
    <section className="mx-auto flex max-w-2xl flex-col items-center justify-center gap-4 py-24 text-center">
      <span className="inline-flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <LayoutList className="h-6 w-6" />
      </span>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Your board is ready</h1>
        <p className="text-sm text-muted-foreground">
          Press <kbd className="rounded border border-border px-1 font-mono text-xs">⌘K</kbd>{" "}
          to open the command palette and create your first task.
        </p>
      </div>
    </section>
  );
}
