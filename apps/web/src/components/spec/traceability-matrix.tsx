import { Check, GitBranch, ListChecks, TestTube2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { RequirementTrace } from "@/lib/api/types";

import { traceSealed } from "./spec-meta";

export interface TraceabilityMatrixProps {
  traces: RequirementTrace[];
}

/** A monospace identifier chip (requirement / acceptance / task / test ref). */
function Ref({ children, tone = "steel" }: { children: string; tone?: "steel" | "muted" }) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center truncate rounded border px-1.5 py-0.5 font-mono text-[11px]",
        tone === "steel"
          ? "border-border bg-muted text-foreground/80"
          : "border-transparent text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function RefList({ refs }: { refs: string[] }) {
  if (refs.length === 0) {
    return <span className="text-xs text-muted-foreground/70">—</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {refs.map((ref) => (
        <Ref key={ref}>{ref}</Ref>
      ))}
    </div>
  );
}

function StateChip({ trace }: { trace: RequirementTrace }) {
  if (traceSealed(trace)) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-success/40 bg-success/10 px-2 py-0.5 text-[11px] font-medium text-success">
        <Check className="h-3 w-3" aria-hidden />
        Sealed
      </span>
    );
  }
  if (trace.satisfied) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] font-medium text-warning">
        Untested
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-danger/40 bg-danger/10 px-2 py-0.5 text-[11px] font-medium text-danger">
      Open
    </span>
  );
}

/**
 * The requirement -> acceptance -> task -> test traceability matrix — the core
 * of spec validation. Each row is a requirement and the chain proving it is
 * covered; a requirement is "sealed" only when it is satisfied and has tests.
 */
export function TraceabilityMatrix({ traces }: TraceabilityMatrixProps) {
  if (traces.length === 0) {
    return (
      <div
        data-testid="traceability-empty"
        className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/40 p-10 text-center"
      >
        <ListChecks className="h-7 w-7 text-muted-foreground" aria-hidden />
        <p className="text-sm font-medium text-foreground">No traceability yet</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Requirements link to tasks and tests once the spec is planned and
          validated. Run validation to seal the chain.
        </p>
      </div>
    );
  }

  const sealed = traces.filter(traceSealed).length;

  return (
    <div className="flex flex-col gap-2">
      <p data-testid="trace-summary" className="text-xs text-muted-foreground">
        <span className="font-mono text-foreground">{sealed}</span> of{" "}
        <span className="font-mono text-foreground">{traces.length}</span>{" "}
        requirements sealed
      </p>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table
          data-testid="traceability-matrix"
          className="w-full min-w-[46rem] border-collapse text-left text-sm"
        >
          <thead>
            <tr className="border-b border-border bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-4 py-2 font-medium">
                Requirement
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                <span className="inline-flex items-center gap-1.5">
                  <ListChecks className="h-3.5 w-3.5" aria-hidden />
                  Acceptance
                </span>
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                <span className="inline-flex items-center gap-1.5">
                  <GitBranch className="h-3.5 w-3.5" aria-hidden />
                  Tasks
                </span>
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                <span className="inline-flex items-center gap-1.5">
                  <TestTube2 className="h-3.5 w-3.5" aria-hidden />
                  Tests
                </span>
              </th>
              <th scope="col" className="px-4 py-2 text-right font-medium">
                State
              </th>
            </tr>
          </thead>
          <tbody>
            {traces.map((trace) => (
              <tr
                key={trace.requirement_id}
                data-testid="trace-row"
                className="border-b border-border/60 align-top last:border-b-0 hover:bg-accent/40"
              >
                <td className="px-4 py-3">
                  <div className="flex flex-col gap-1">
                    <span className="font-mono text-xs font-medium text-primary">
                      {trace.requirement_id}
                    </span>
                    {trace.text ? (
                      <span className="max-w-md text-sm text-foreground">
                        {trace.text}
                      </span>
                    ) : null}
                  </div>
                </td>
                <td className="px-4 py-3">
                  <RefList refs={trace.acceptance_criteria_ids ?? []} />
                </td>
                <td className="px-4 py-3">
                  <RefList refs={trace.task_refs ?? []} />
                </td>
                <td className="px-4 py-3">
                  {(trace.test_refs?.length ?? 0) > 0 ? (
                    <RefList refs={trace.test_refs ?? []} />
                  ) : (
                    <span className="text-xs text-danger/80">no tests</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right">
                  <StateChip trace={trace} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
