"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useCreateEpic, useEpics } from "@/lib/api/hooks";
import { useCreateSpec } from "@/lib/api/spec";
import type { SpecDraft, SpecManifest } from "@/lib/api/types";
import { applySpecTemplate, SPEC_TEMPLATES, type SpecTemplateId } from "@/lib/spec-studio/templates";
import { cn } from "@/lib/utils";

import { AiDraftPanel } from "./ai-draft-panel";
import { GuidedMode } from "./guided-mode";

type EntryMode = "scratch" | "ai";

/** Sentinel `<select>` value that reveals the inline "new epic" text field. */
const NEW_EPIC_VALUE = "__new_epic__";

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
  const searchParams = useSearchParams();
  const epicIdFromQuery = searchParams?.get("epicId") ?? "";

  const epicsQuery = useEpics(client);
  const createEpic = useCreateEpic(client);
  const createSpec = useCreateSpec(client);

  const [epicId, setEpicId] = useState(epicIdFromQuery);
  const [newEpicTitle, setNewEpicTitle] = useState("");
  const [draft, setDraft] = useState<SpecManifest>({ id: "", name: "" });
  const [entryMode, setEntryMode] = useState<EntryMode>("scratch");
  const [templateId, setTemplateId] = useState<SpecTemplateId | null>(null);

  const epics = useMemo(() => epicsQuery.data ?? [], [epicsQuery.data]);
  const creatingNewEpic = epicId === NEW_EPIC_VALUE;

  function handleAiDraft(result: SpecDraft) {
    if (result.manifest) {
      // Draft-only preview: keep the draft's own placeholder id blank until
      // the spec is actually created — everything else the model wrote
      // (name, requirements, acceptance criteria, ...) seeds Guided mode.
      setDraft({ ...result.manifest, id: "" });
    }
  }

  function handleTemplate(next: SpecTemplateId) {
    setTemplateId(next);
    setDraft((current) => applySpecTemplate(next, current));
  }

  const hasEpicTarget = creatingNewEpic
    ? newEpicTitle.trim().length > 0
    : Boolean(epicId);
  const canCreate =
    hasEpicTarget &&
    draft.name.trim().length > 0 &&
    !createSpec.isPending &&
    !createEpic.isPending;

  function createSpecFor(resolvedEpicId: string) {
    createSpec.mutate(
      {
        epic_id: resolvedEpicId,
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
          toast.success(`Created ${created.name || "spec"}`);
          if (onCreated) onCreated(created.id);
          else router.push(`/specs/${encodeURIComponent(created.id)}`);
        },
      },
    );
  }

  function handleCreate() {
    if (!canCreate) return;
    if (creatingNewEpic) {
      createEpic.mutate(
        { title: newEpicTitle.trim() },
        {
          onSuccess: (epic) => {
            if (epic.id) createSpecFor(epic.id);
          },
        },
      );
    } else {
      createSpecFor(epicId);
    }
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
        <AiDraftPanel
          epicId={creatingNewEpic ? undefined : epicId || undefined}
          client={client}
          onDraft={handleAiDraft}
        />
      ) : null}

      {entryMode === "scratch" ? (
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium text-foreground">
            Start from a template
          </legend>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Starter templates">
            {SPEC_TEMPLATES.map((template) => (
              <button
                key={template.id}
                type="button"
                data-testid={`spec-template-${template.id}`}
                title={template.description}
                aria-pressed={templateId === template.id}
                onClick={() => handleTemplate(template.id)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  templateId === template.id
                    ? "border-primary/40 bg-accent text-foreground"
                    : "border-border text-muted-foreground hover:text-foreground",
                )}
              >
                {template.label}
              </button>
            ))}
          </div>
        </fieldset>
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
          <option value={NEW_EPIC_VALUE}>+ Create new epic…</option>
        </select>
      </label>

      {creatingNewEpic ? (
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium text-foreground">New epic title</span>
          <input
            type="text"
            data-testid="new-spec-new-epic-title"
            value={newEpicTitle}
            onChange={(event) => setNewEpicTitle(event.target.value)}
            placeholder="e.g. Billing v3"
            className={cn(
              "rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none",
              "focus-visible:ring-2 focus-visible:ring-ring",
            )}
          />
        </label>
      ) : null}

      <GuidedMode
        value={draft}
        onChange={setDraft}
        onSave={handleCreate}
        saving={createSpec.isPending || createEpic.isPending}
        dirty={canCreate}
        saveError={
          createSpec.isError
            ? errorMessage(createSpec.error)
            : createEpic.isError
              ? errorMessage(createEpic.error)
              : null
        }
      />

      <div className="flex justify-end">
        <Button
          onClick={handleCreate}
          disabled={!canCreate}
          data-testid="create-spec"
        >
          {createSpec.isPending || createEpic.isPending ? "Creating…" : "Create spec"}
        </Button>
      </div>
    </div>
  );
}
