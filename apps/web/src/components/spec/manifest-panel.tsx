import { CircleDot, FileText, GitFork } from "lucide-react";

import { cn } from "@/lib/utils";
import type { SpecManifest } from "@/lib/api/types";

export interface ManifestPanelProps {
  spec: SpecManifest;
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="text-sm text-foreground">{children}</dd>
    </div>
  );
}

function Mono({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-muted-foreground/70">—</span>;
  return <span className="font-mono text-xs text-foreground/90">{value}</span>;
}

const EXECUTION_MODE_LABELS: Record<string, string> = {
  single_agent: "Single agent",
  supervised_multi_agent: "Supervised multi-agent",
};

/**
 * The raw spec manifest: execution facts, referenced artifacts, constraints,
 * open questions and the architecture decisions (ADRs) recorded during
 * planning. The read-only counterpart to the traceability + gate views.
 */
export function ManifestPanel({ spec }: ManifestPanelProps) {
  const repos = spec.repos ?? [];
  const constraints = spec.constraints ?? [];
  const openQuestions = spec.open_questions ?? [];
  const decisions = spec.decisions ?? [];

  return (
    <div data-testid="manifest-panel" className="flex flex-col gap-6">
      <section className="rounded-lg border border-border bg-card/60 p-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Fact label="Execution mode">
            {spec.execution_mode
              ? EXECUTION_MODE_LABELS[spec.execution_mode] ?? spec.execution_mode
              : "—"}
          </Fact>
          <Fact label="Skill profile">
            <Mono value={spec.skill_profile} />
          </Fact>
          <Fact label="Spec id">
            <Mono value={spec.id} />
          </Fact>
          <Fact label="Plan">
            <Mono value={spec.plan_ref} />
          </Fact>
          <Fact label="Tasks">
            <Mono value={spec.tasks_ref} />
          </Fact>
          <Fact label="Validation">
            <Mono value={spec.validation_ref} />
          </Fact>
        </dl>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="flex items-center gap-2 font-display text-sm font-semibold tracking-tight text-foreground">
          <GitFork className="h-4 w-4 text-primary" aria-hidden />
          Repositories
        </h3>
        {repos.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {repos.map((repo) => (
              <span
                key={repo}
                className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-xs text-foreground/80"
              >
                {repo}
              </span>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">No repositories targeted.</p>
        )}
      </section>

      {constraints.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
            Constraints
          </h3>
          <ul className="flex flex-col gap-1.5">
            {constraints.map((constraint) => (
              <li
                key={constraint}
                className="flex items-start gap-2 text-sm text-muted-foreground"
              >
                <span
                  aria-hidden
                  className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-warning"
                />
                {constraint}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="flex flex-col gap-2">
        <h3 className="flex items-center gap-2 font-display text-sm font-semibold tracking-tight text-foreground">
          <CircleDot className="h-4 w-4 text-primary" aria-hidden />
          Open questions
        </h3>
        {openQuestions.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {openQuestions.map((question) => {
              const resolved = Boolean(question.resolution);
              return (
                <li
                  key={question.id}
                  className={cn(
                    "flex flex-col gap-1 rounded-lg border px-4 py-3",
                    resolved
                      ? "border-border bg-card/60"
                      : "border-warning/40 bg-warning/10",
                  )}
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {question.id}
                    </span>
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                        resolved
                          ? "bg-success/10 text-success"
                          : "bg-warning/20 text-warning",
                      )}
                    >
                      {resolved ? "Resolved" : "Open"}
                    </span>
                  </div>
                  <p className="text-sm text-foreground">{question.text}</p>
                  {question.resolution ? (
                    <p className="text-xs text-muted-foreground">
                      {question.resolution}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">
            All questions resolved — nothing blocking clarification.
          </p>
        )}
      </section>

      {decisions.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="flex items-center gap-2 font-display text-sm font-semibold tracking-tight text-foreground">
            <FileText className="h-4 w-4 text-primary" aria-hidden />
            Architecture decisions
          </h3>
          <div className="flex flex-col gap-2">
            {decisions.map((adr) => (
              <article
                key={adr.id}
                className="flex flex-col gap-1.5 rounded-lg border border-border bg-card/60 px-4 py-3"
              >
                <div className="flex items-center justify-between gap-2">
                  <h4 className="font-display text-sm font-semibold text-foreground">
                    <span className="font-mono text-xs text-muted-foreground">
                      {adr.id}
                    </span>{" "}
                    {adr.title}
                  </h4>
                  {adr.status ? (
                    <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                      {adr.status}
                    </span>
                  ) : null}
                </div>
                {adr.decision ? (
                  <p className="text-sm text-muted-foreground">{adr.decision}</p>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
