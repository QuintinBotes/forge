"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useEpics } from "@/lib/api/hooks";
import { useCreateSpec } from "@/lib/api/spec";
import type { SpecDraft, SpecManifest } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { AiDraftPanel } from "./ai-draft-panel";
import { GuidedMode } from "./guided-mode";

type EntryMode = "scratch" | "ai";

export interface NewSpecPageProps {
  client?: ForgeApiClient;
  /** Navigate to the created spec (defaults to router push to /specs/{id}). */
  onCreated?: (specId: string) => void;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}

/**
 * `/specs/new` — the guided spec-creation entry point. Pick the epic the spec
 * belongs to, then draft the manifest in the same Guided-mode form used to
 * edit an existing spec, so authoring feels identical whether you're
 * starting fresh or refining later. On create, hands off to `/specs/{id}`
 * where the full four-mode Spec Studio (Guided/Markdown/YAML/Read) takes over.
 */
export function NewSpecPage({
  client = apiClient,
  onCreated,
}: NewSpecPageProps) {
  const router = useRouter();
  const epicsQuery = useEpics(client);
  const createSpec = useCreateSpec(client);

  const [epicId, setEpicId] = useState("");
  const [draft, setDraft] = useState<SpecManifest>({ id: "", name: "" });
  const [entryMode, setEntryMode] = useState<EntryMode>("scratch");

  const epics = epicsQuery.data ?? [];

  function handleAiDraft(result: SpecDraft) {
    if (result.manifest) {
      // Draft-only preview: keep the draft's own placeholder id blank until
      // the spec is actually created — everything else the model wrote
      // (name, requirements, acceptance criteria, ...) seeds Guided mode.
      setDraft({ ...result.manifest, id: "" });
    }
  }
  const canCreate =
    Boolean(epicId) && draft.name.trim().length > 0 && !createSpec.isPending;

  function handleCreate() {
    if (!canCreate) return;
    createSpec.mutate(
      {
        epic_id: epicId,
        name: draft.name,
        requirements: draft.requirements,
        acceptance_criteria: draft.acceptance_criteria,
        open_questions: draft.open_questions,
        constraints: draft.constraints,
        decisions: draft.decisions,
        execution_mode: draft.execution_mode,
        constitution_refs: draft.constitution_refs,
        repos: draft.repos,
      },
      {
        onSuccess: (created) => {
          if (onCreated) onCreated(created.id);
          else router.push(`/specs/${encodeURIComponent(created.id)}`);
        },
      },
    );
  }

  return (
    <div className="flex flex-col gap-5" data-testid="new-spec-page">
      <header className="flex flex-col gap-1">
        <h1 className="font-display text-xl font-semibold tracking-tight text-foreground">
          New spec
        </h1>
        <p className="text-sm text-muted-foreground">
          Draft the goal, requirements and acceptance criteria — refine and
          switch to Markdown or YAML any time after it&rsquo;s created.
        </p>
      </header>

      <div
        role="tablist"
        aria-label="New spec entry mode"
        className="inline-flex w-fit items-center gap-1 rounded-lg border border-border bg-muted/50 p-1"
      >
        {(
          [
            { id: "scratch", label: "Start from scratch" },
            { id: "ai", label: "Draft with AI" },
          ] as const
        ).map((option) => (
          <button
            key={option.id}
            role="tab"
            type="button"
            aria-selected={entryMode === option.id}
            onClick={() => setEntryMode(option.id)}
            data-testid={`new-spec-entry-${option.id}`}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              entryMode === option.id
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>

      {entryMode === "ai" ? (
        <AiDraftPanel epicId={epicId || undefined} client={client} onDraft={handleAiDraft} />
      ) : null}

      <label className="flex flex-col gap-1.5 text-sm">
        <span className="font-medium text-foreground">Epic</span>
        <select
          data-testid="new-spec-epic"
          value={epicId}
          onChange={(event) => setEpicId(event.target.value)}
          disabled={epicsQuery.isLoading}
          className={cn(
            "rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none",
            "focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <option value="">Select an epic…</option>
          {epics.map((epic) => (
            <option key={epic.id} value={epic.id ?? ""}>
              {epic.title}
            </option>
          ))}
        </select>
      </label>

      <GuidedMode
        value={draft}
        onChange={setDraft}
        onSave={handleCreate}
        saving={createSpec.isPending}
        dirty={canCreate}
        saveError={createSpec.isError ? errorMessage(createSpec.error) : null}
      />

      <div className="flex justify-end">
        <Button
          onClick={handleCreate}
          disabled={!canCreate}
          data-testid="create-spec"
        >
          {createSpec.isPending ? "Creating…" : "Create spec"}
        </Button>
      </div>
    </div>
  );
}
