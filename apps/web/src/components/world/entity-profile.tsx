"use client";

/**
 * The focused entity's profile: its label, type and other names; the facts
 * stated about it (and the facts that reference it) as a table with valid
 * time on the world clock, recorded time on the system clock, confidence
 * and source; each fact's evidence quotes behind an expander; and the
 * entity's record-axis change timeline below.
 */

import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import type { ChangeOut, EntityProfileOut, FactOut } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  errorDetail,
  formatConfidence,
  formatTimestamp,
  formatValidRange,
  shortId,
} from "./world-format";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface EntityProfileProps {
  profile: EntityProfileOut;
  /** Best known label for an entity id (neighbour labels, search results). */
  resolveLabel: (entityId: string) => string;
  onFocusEntity: (entityId: string) => void;
  changes?: ChangeOut[];
  changesIsLoading?: boolean;
  changesError?: unknown;
  onRetryChanges?: () => void;
}

function FactValue({
  fact,
  linkEntityId,
  resolveLabel,
  onFocusEntity,
}: {
  fact: FactOut;
  /** The entity this row links to: the object, or the subject for incoming. */
  linkEntityId: string | null;
  resolveLabel: (entityId: string) => string;
  onFocusEntity: (entityId: string) => void;
}) {
  if (linkEntityId) {
    const label = resolveLabel(linkEntityId);
    return (
      <button
        type="button"
        onClick={() => onFocusEntity(linkEntityId)}
        aria-label={`Focus ${label}`}
        className="rounded-sm text-left text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {label}
      </button>
    );
  }
  return <span>{fact.value ?? "—"}</span>;
}

function FactsTable({
  facts,
  linkEntityIdFor,
  resolveLabel,
  onFocusEntity,
  expanded,
  onToggleEvidence,
}: {
  facts: FactOut[];
  linkEntityIdFor: (fact: FactOut) => string | null;
  resolveLabel: (entityId: string) => string;
  onFocusEntity: (entityId: string) => void;
  expanded: Set<string>;
  onToggleEvidence: (factId: string) => void;
}) {
  if (facts.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No facts at this time.</p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            <th scope="col" className="py-2 pr-3 font-medium">
              Relation
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Value
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Valid time
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Recorded
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Confidence
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Source
            </th>
            <th scope="col" className="py-2 font-medium">
              Evidence
            </th>
          </tr>
        </thead>
        <tbody>
          {facts.map((fact) => {
            const isExpanded = expanded.has(fact.id);
            const relation = fact.phrase || fact.predicate_key || "—";
            return (
              <Fragment key={fact.id}>
                <tr className="border-b border-border align-top">
                  <td className="py-2 pr-3">{relation}</td>
                  <td className="py-2 pr-3">
                    <FactValue
                      fact={fact}
                      linkEntityId={linkEntityIdFor(fact)}
                      resolveLabel={resolveLabel}
                      onFocusEntity={onFocusEntity}
                    />
                  </td>
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {formatValidRange(fact)}
                  </td>
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {formatTimestamp(fact.recorded_at)}
                  </td>
                  <td className="py-2 pr-3">
                    {formatConfidence(fact.confidence)}
                  </td>
                  <td className="py-2 pr-3" title={fact.actor}>
                    {fact.source_kind} ·{" "}
                    {UUID.test(fact.actor) ? shortId(fact.actor) : fact.actor}
                  </td>
                  <td className="py-2">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={fact.evidence.length === 0}
                      aria-expanded={isExpanded}
                      aria-label={`Evidence for ${relation}: ${fact.evidence.length}`}
                      onClick={() => onToggleEvidence(fact.id)}
                    >
                      {isExpanded ? (
                        <ChevronDown aria-hidden />
                      ) : (
                        <ChevronRight aria-hidden />
                      )}
                      {fact.evidence.length}
                    </Button>
                  </td>
                </tr>
                {isExpanded ? (
                  <tr
                    key={`${fact.id}-evidence`}
                    className="border-b border-border"
                  >
                    <td colSpan={7} className="py-2">
                      <ul className="flex flex-col gap-2">
                        {fact.evidence.map((evidence) => (
                          <li key={evidence.id}>
                            <blockquote className="text-sm text-foreground">
                              “{evidence.quote}”
                            </blockquote>
                            <p className="font-mono text-xs text-muted-foreground">
                              {evidence.source_ref}
                            </p>
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  );
}

export function EntityProfile({
  profile,
  resolveLabel,
  onFocusEntity,
  changes,
  changesIsLoading = false,
  changesError,
  onRetryChanges,
}: EntityProfileProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const { entity, names, facts, object_of: objectOf } = profile;
  const otherNames = names.filter((name) => name !== entity.label);

  const toggleEvidence = (factId: string) => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(factId)) {
        next.delete(factId);
      } else {
        next.add(factId);
      }
      return next;
    });
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h2 className="font-display text-lg font-semibold tracking-tight text-foreground">
          {entity.label}
        </h2>
        {entity.type_key ? (
          <p className="text-sm text-muted-foreground">{entity.type_key}</p>
        ) : null}
        {otherNames.length > 0 ? (
          <p className="text-sm text-muted-foreground">
            Also known as: {otherNames.join(", ")}
          </p>
        ) : null}
        {profile.resolved_id !== entity.id ? (
          <p className="text-sm text-muted-foreground">
            Resolved to this entity through a merge.
          </p>
        ) : null}
      </div>

      <Section title="Facts">
        <FactsTable
          facts={facts}
          linkEntityIdFor={(fact) => fact.object_id}
          resolveLabel={resolveLabel}
          onFocusEntity={onFocusEntity}
          expanded={expanded}
          onToggleEvidence={toggleEvidence}
        />
      </Section>

      {objectOf.length > 0 ? (
        <Section title="Referenced by">
          <FactsTable
            facts={objectOf}
            linkEntityIdFor={(fact) => fact.subject_id}
            resolveLabel={resolveLabel}
            onFocusEntity={onFocusEntity}
            expanded={expanded}
            onToggleEvidence={toggleEvidence}
          />
        </Section>
      ) : null}

      <Section title="Changes">
        {changesIsLoading ? (
          <Loading label="Loading changes…">
            <div className="flex flex-col gap-2">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          </Loading>
        ) : changesError ? (
          <ErrorState
            title="Changes didn't load"
            description={errorDetail(changesError)}
            onRetry={onRetryChanges}
          />
        ) : !changes || changes.length === 0 ? (
          <p className="text-sm text-muted-foreground">No changes recorded.</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {changes.map((change) => (
              <li
                key={`${change.fact_id}-${change.occurred_at}-${change.kind}`}
                className="text-sm"
              >
                <span className="font-medium text-foreground">
                  {change.kind}
                </span>{" "}
                <span className="text-muted-foreground">
                  {formatTimestamp(change.occurred_at)}
                </span>
                {change.reason ? (
                  <span className={cn("block text-muted-foreground")}>
                    {change.reason}
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </Section>
    </div>
  );
}
