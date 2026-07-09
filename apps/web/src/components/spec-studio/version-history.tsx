"use client";

import { GitCompare, History, Minus, Plus, RefreshCw } from "lucide-react";
import { useState } from "react";

import { useSpecVersionDiff, useSpecVersions } from "@/lib/api/spec-versions";
import type { ForgeApiClient } from "@/lib/api/client";
import { apiClient } from "@/lib/api/client";
import type { ListItemChange } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface VersionHistoryProps {
  specId: string;
  client?: ForgeApiClient;
}

const FIELD_LABELS: Record<string, string> = {
  requirements: "Requirements",
  acceptance_criteria: "Acceptance criteria",
  open_questions: "Open questions",
  decisions: "Decisions",
};

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function ChangeBadge({ change }: { change: ListItemChange["change"] }) {
  const label = change === "added" ? "added" : change === "removed" ? "removed" : "modified";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        change === "added" && "bg-success/10 text-success",
        change === "removed" && "bg-danger/10 text-danger",
        change === "modified" && "bg-warning/10 text-warning",
      )}
    >
      {label}
    </span>
  );
}

function ListFieldDiff({ field, changes }: { field: string; changes: ListItemChange[] }) {
  if (changes.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5" data-testid={`diff-field-${field}`}>
      <p className="text-xs font-semibold text-foreground">{FIELD_LABELS[field] ?? field}</p>
      <ul className="flex flex-col gap-1">
        {changes.map((change) => (
          <li key={change.id} className="flex items-start gap-2 text-xs">
            <ChangeBadge change={change.change} />
            <span className="font-mono text-muted-foreground">{change.id}</span>
            {change.change === "modified" ? (
              <span className="text-muted-foreground">
                {String(change.before?.text ?? "")} → {String(change.after?.text ?? "")}
              </span>
            ) : (
              <span className="text-muted-foreground">
                {String((change.after ?? change.before)?.text ?? "")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Spec Studio's version history + diff (ss-versioning). Every save (Guided /
 * Markdown / YAML) records an immutable snapshot on the backend; this surface
 * lists them and diffs any two — a line-level `spec.md` diff plus a
 * structured manifest diff (id-keyed adds/removes/changes per list field).
 */
export function VersionHistory({ specId, client = apiClient }: VersionHistoryProps) {
  const versionsQuery = useSpecVersions(specId, client);
  const versions = versionsQuery.data ?? [];

  const [fromVersion, setFromVersion] = useState<number | null>(null);
  const [toVersion, setToVersion] = useState<number | null>(null);
  // Tracks whether the "previous vs latest" default has been applied for this
  // spec's version list, using React's "adjust state while rendering" pattern
  // (mirrors `SpecStudio`'s per-`specId` override reset) rather than an effect
  // that would set state a render late.
  const [defaultedFor, setDefaultedFor] = useState<string | null>(null);
  if (versions.length > 0 && defaultedFor !== specId) {
    setDefaultedFor(specId);
    setToVersion(versions[0]?.version_number ?? null);
    setFromVersion(versions[1]?.version_number ?? versions[0]?.version_number ?? null);
  }

  const diffQuery = useSpecVersionDiff(specId, fromVersion, toVersion, client);
  const diff = diffQuery.data;

  if (versionsQuery.isLoading) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="version-history-loading">
        Loading version history…
      </p>
    );
  }

  if (versions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="version-history-empty">
        No versions yet — save the spec to record its first version.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="version-history">
      <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
          <History className="h-3.5 w-3.5" aria-hidden />
          Versions
        </div>
        <ul className="flex flex-col gap-1">
          {versions.map((version) => (
            <li
              key={version.version_number}
              className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-xs hover:bg-muted/50"
              data-testid={`version-row-${version.version_number}`}
            >
              <span className="flex items-center gap-2">
                <span className="font-mono font-medium text-foreground">
                  v{version.version_number}
                </span>
                <span className="text-muted-foreground">{version.name}</span>
                <span className="text-muted-foreground">· {version.status}</span>
              </span>
              <span className="text-muted-foreground">{formatTimestamp(version.created_at)}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <GitCompare className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
          <span className="font-semibold text-foreground">Compare</span>
          <label className="flex items-center gap-1.5">
            <span className="text-muted-foreground">From</span>
            <select
              aria-label="Compare from version"
              data-testid="diff-from-select"
              className="rounded-md border border-border bg-background px-1.5 py-1 text-xs"
              value={fromVersion ?? ""}
              onChange={(event) => setFromVersion(Number(event.target.value))}
            >
              {versions.map((v) => (
                <option key={v.version_number} value={v.version_number}>
                  v{v.version_number}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            <span className="text-muted-foreground">To</span>
            <select
              aria-label="Compare to version"
              data-testid="diff-to-select"
              className="rounded-md border border-border bg-background px-1.5 py-1 text-xs"
              value={toVersion ?? ""}
              onChange={(event) => setToVersion(Number(event.target.value))}
            >
              {versions.map((v) => (
                <option key={v.version_number} value={v.version_number}>
                  v{v.version_number}
                </option>
              ))}
            </select>
          </label>
        </div>

        {diffQuery.isLoading ? (
          <p className="text-xs text-muted-foreground" data-testid="diff-loading">
            <RefreshCw className="mr-1 inline h-3 w-3 animate-spin" aria-hidden />
            Diffing versions…
          </p>
        ) : null}

        {diff ? (
          <div className="flex flex-col gap-4" data-testid="diff-panel">
            {!diff.manifest.scalar_changes.length &&
            !diff.manifest.requirements.length &&
            !diff.manifest.acceptance_criteria.length &&
            !diff.manifest.open_questions.length &&
            !diff.manifest.decisions.length &&
            !diff.manifest.constraints_added.length &&
            !diff.manifest.constraints_removed.length ? (
              <p className="text-xs text-muted-foreground" data-testid="diff-no-changes">
                No manifest changes between v{diff.from_version} and v{diff.to_version}.
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                {diff.manifest.scalar_changes.length > 0 ? (
                  <div className="flex flex-col gap-1">
                    {diff.manifest.scalar_changes.map((change) => (
                      <p key={change.field} className="text-xs">
                        <span className="font-semibold text-foreground">{change.field}</span>{" "}
                        <span className="text-danger">{String(change.before)}</span>{" "}
                        <span className="text-muted-foreground">→</span>{" "}
                        <span className="text-success">{String(change.after)}</span>
                      </p>
                    ))}
                  </div>
                ) : null}
                <ListFieldDiff field="requirements" changes={diff.manifest.requirements} />
                <ListFieldDiff
                  field="acceptance_criteria"
                  changes={diff.manifest.acceptance_criteria}
                />
                <ListFieldDiff field="open_questions" changes={diff.manifest.open_questions} />
                <ListFieldDiff field="decisions" changes={diff.manifest.decisions} />
                {diff.manifest.constraints_added.length > 0 ||
                diff.manifest.constraints_removed.length > 0 ? (
                  <div className="flex flex-col gap-1">
                    <p className="text-xs font-semibold text-foreground">Constraints</p>
                    {diff.manifest.constraints_added.map((text) => (
                      <p key={`added-${text}`} className="flex items-center gap-1 text-xs text-success">
                        <Plus className="h-3 w-3" aria-hidden />
                        {text}
                      </p>
                    ))}
                    {diff.manifest.constraints_removed.map((text) => (
                      <p key={`removed-${text}`} className="flex items-center gap-1 text-xs text-danger">
                        <Minus className="h-3 w-3" aria-hidden />
                        {text}
                      </p>
                    ))}
                  </div>
                ) : null}
              </div>
            )}

            <div className="flex flex-col gap-1">
              <p className="text-xs font-semibold text-foreground">spec.md</p>
              <pre
                className="max-h-96 overflow-auto rounded-md border border-border bg-muted/30 p-2 font-mono text-xs leading-relaxed"
                data-testid="diff-markdown"
              >
                {diff.markdown.map((line, index) => (
                  <div
                    key={index}
                    className={cn(
                      "whitespace-pre-wrap px-1",
                      line.op === "insert" && "bg-success/10 text-success",
                      line.op === "delete" && "bg-danger/10 text-danger",
                    )}
                  >
                    {line.op === "insert" ? "+ " : line.op === "delete" ? "- " : "  "}
                    {line.text}
                  </div>
                ))}
              </pre>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
