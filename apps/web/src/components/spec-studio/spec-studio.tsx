"use client";

import { Eye, FileCode2, FileText, History, ListTree } from "lucide-react";
import { useState } from "react";

import { apiClient, ApiError, type ForgeApiClient } from "@/lib/api/client";
import { useApproveSpec } from "@/lib/api/spec";
import {
  useSaveGuidedManifest,
  useSaveSpecMarkdown,
  useSaveSpecManifestYaml,
  useSpecStudioManifest,
  useSpecStudioMarkdown,
  useSpecStudioYaml,
} from "@/lib/api/spec-studio";
import type { SpecManifest } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { GuidedMode } from "./guided-mode";
import { MarkdownMode } from "./markdown-mode";
import { ReadMode } from "./read-mode";
import { VersionHistory } from "./version-history";
import { YamlMode } from "./yaml-mode";

export type SpecStudioMode = "guided" | "markdown" | "yaml" | "read" | "history";

const MODES: { id: SpecStudioMode; label: string; icon: typeof ListTree }[] = [
  { id: "guided", label: "Guided", icon: ListTree },
  { id: "markdown", label: "Markdown", icon: FileText },
  { id: "yaml", label: "YAML", icon: FileCode2 },
  { id: "read", label: "Read", icon: Eye },
  { id: "history", label: "History", icon: History },
];

export interface SpecStudioProps {
  specId: string;
  client?: ForgeApiClient;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}

/**
 * Spec Studio — the spec-authoring surface over one canonical `SpecManifest`,
 * editable from four modes: **Guided** (a structured form), **Markdown**
 * (`spec.md` prose — the default human/agent surface), **YAML**
 * (`manifest.yaml` — the precise machine/CI/agent surface, schema-aware with
 * live validation), and **Read** (the rendered, read-only manifest).
 *
 * All three editable modes round-trip through the same `SpecManifest` on the
 * backend (`forge_spec.FileSpecEngine`): saving in any one re-renders the
 * other two to match. Each mode keeps its own uncommitted-edit "override" in
 * local state (not the query cache) so switching tabs never discards unsaved
 * work; a successful save clears that mode's override and invalidates the
 * *other* two modes' queries, so the next visit reloads the freshly synced
 * text rather than stale content.
 */
export function SpecStudio({ specId, client = apiClient }: SpecStudioProps) {
  const [mode, setMode] = useState<SpecStudioMode>("guided");

  // Reset per-spec overrides during render when `specId` changes (React's
  // "adjust state while rendering" pattern for resetting state on a prop
  // change) rather than in an effect.
  const [activeSpecId, setActiveSpecId] = useState(specId);
  const [guidedOverride, setGuidedOverride] = useState<SpecManifest | null>(null);
  const [markdownOverride, setMarkdownOverride] = useState<string | null>(null);
  const [yamlOverride, setYamlOverride] = useState<string | null>(null);
  if (specId !== activeSpecId) {
    setActiveSpecId(specId);
    setGuidedOverride(null);
    setMarkdownOverride(null);
    setYamlOverride(null);
  }

  const manifestQuery = useSpecStudioManifest(specId, client);
  const markdownQuery = useSpecStudioMarkdown(specId, mode === "markdown", client);
  const yamlQuery = useSpecStudioYaml(specId, mode === "yaml", client);

  const saveGuided = useSaveGuidedManifest(specId, client);
  const saveMarkdown = useSaveSpecMarkdown(specId, client);
  const saveYaml = useSaveSpecManifestYaml(specId, client);
  const approveSpec = useApproveSpec(client);

  const manifest = manifestQuery.data ?? null;
  const guidedValue = guidedOverride ?? manifest;
  const guidedDirty = Boolean(
    manifest && guidedOverride && JSON.stringify(manifest) !== JSON.stringify(guidedOverride),
  );

  const markdownValue = markdownOverride ?? markdownQuery.data ?? null;
  const markdownDirty = Boolean(
    markdownQuery.data !== undefined && markdownOverride !== null && markdownOverride !== markdownQuery.data,
  );

  const yamlValue = yamlOverride ?? yamlQuery.data ?? null;
  const yamlDirty = Boolean(
    yamlQuery.data !== undefined && yamlOverride !== null && yamlOverride !== yamlQuery.data,
  );

  const loadError = manifestQuery.isError
    ? errorMessage(manifestQuery.error)
    : mode === "markdown" && markdownQuery.isError
      ? errorMessage(markdownQuery.error)
      : mode === "yaml" && yamlQuery.isError
        ? errorMessage(yamlQuery.error)
        : null;

  return (
    <div className="flex flex-col gap-4" data-testid="spec-studio">
      <div
        role="tablist"
        aria-label="Spec Studio mode"
        className="inline-flex w-fit items-center gap-1 rounded-lg border border-border bg-muted/50 p-1"
      >
        {MODES.map((m) => {
          const Icon = m.icon;
          const selected = m.id === mode;
          return (
            <button
              key={m.id}
              role="tab"
              type="button"
              aria-selected={selected}
              onClick={() => setMode(m.id)}
              data-testid={`studio-mode-${m.id}`}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selected ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden />
              {m.label}
            </button>
          );
        })}
      </div>

      {loadError ? (
        <p role="status" data-testid="studio-error" className="text-xs text-muted-foreground">
          {loadError}
        </p>
      ) : null}

      {manifestQuery.isLoading || !manifest ? (
        <p className="text-sm text-muted-foreground" data-testid="studio-loading">
          Loading spec…
        </p>
      ) : (
        <>
          {mode === "guided" && guidedValue ? (
            <GuidedMode
              value={guidedValue}
              onChange={setGuidedOverride}
              onSave={() => {
                if (guidedOverride) {
                  saveGuided.mutate(guidedOverride, { onSuccess: () => setGuidedOverride(null) });
                }
              }}
              saving={saveGuided.isPending}
              dirty={guidedDirty}
              saveError={saveGuided.isError ? errorMessage(saveGuided.error) : null}
            />
          ) : null}
          {mode === "markdown" ? (
            markdownQuery.isLoading || markdownValue === null ? (
              <p className="text-sm text-muted-foreground" data-testid="markdown-loading">
                Loading spec.md…
              </p>
            ) : (
              <MarkdownMode
                value={markdownValue}
                onChange={setMarkdownOverride}
                onSave={() => {
                  if (markdownOverride !== null) {
                    saveMarkdown.mutate(markdownOverride, {
                      onSuccess: () => setMarkdownOverride(null),
                    });
                  }
                }}
                saving={saveMarkdown.isPending}
                dirty={markdownDirty}
                saveError={saveMarkdown.isError ? errorMessage(saveMarkdown.error) : null}
              />
            )
          ) : null}
          {mode === "yaml" ? (
            yamlQuery.isLoading || yamlValue === null ? (
              <p className="text-sm text-muted-foreground" data-testid="yaml-loading">
                Loading manifest.yaml…
              </p>
            ) : (
              <YamlMode
                value={yamlValue}
                onChange={setYamlOverride}
                onSave={() => {
                  if (yamlOverride !== null) {
                    saveYaml.mutate(yamlOverride, { onSuccess: () => setYamlOverride(null) });
                  }
                }}
                saving={saveYaml.isPending}
                dirty={yamlDirty}
                saveError={saveYaml.isError ? errorMessage(saveYaml.error) : null}
              />
            )
          ) : null}
          {mode === "read" ? (
            <ReadMode
              spec={manifest}
              onApprove={() => approveSpec.mutate({ specId })}
              approving={approveSpec.isPending}
              approveError={approveSpec.isError ? errorMessage(approveSpec.error) : null}
            />
          ) : null}
          {mode === "history" ? <VersionHistory specId={specId} client={client} /> : null}
        </>
      )}
    </div>
  );
}
