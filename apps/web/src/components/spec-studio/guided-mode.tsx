"use client";

import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { SPEC_STATUSES, type SpecManifest, type SpecStatus } from "@/lib/api/types";

export interface GuidedModeProps {
  /** The current draft manifest (controlled). */
  value: SpecManifest;
  onChange: (next: SpecManifest) => void;
  onSave: () => void;
  saving?: boolean;
  dirty?: boolean;
  saveError?: string | null;
}

/**
 * The Guided mode — a structured form over the same `SpecManifest` the
 * Markdown and YAML modes edit. The friendliest surface: name, status,
 * requirements and constraints as plain fields/lists rather than prose or
 * YAML syntax.
 */
export function GuidedMode({ value, onChange, onSave, saving = false, dirty = false, saveError }: GuidedModeProps) {
  const requirements = value.requirements ?? [];
  const constraints = value.constraints ?? [];

  return (
    <div className="flex flex-col gap-5" data-testid="guided-mode">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">
          {dirty ? "Unsaved changes" : "Guided"}
        </span>
        <Button size="sm" onClick={onSave} disabled={saving || !dirty} data-testid="guided-save">
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>

      <label className="flex flex-col gap-1.5 text-sm">
        <span className="font-medium text-foreground">Name</span>
        <input
          data-testid="guided-name"
          value={value.name}
          onChange={(event) => onChange({ ...value, name: event.target.value })}
          className="rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </label>

      <label className="flex flex-col gap-1.5 text-sm">
        <span className="font-medium text-foreground">Status</span>
        <select
          data-testid="guided-status"
          value={value.status ?? "draft"}
          onChange={(event) => onChange({ ...value, status: event.target.value as SpecStatus })}
          className="rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {SPEC_STATUSES.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
      </label>

      <section className="flex flex-col gap-2">
        <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
          Requirements
        </h3>
        <ul className="flex flex-col gap-2" data-testid="guided-requirements">
          {requirements.map((req, index) => (
            <li key={req.id || index} className="flex items-center gap-2">
              <input
                aria-label={`Requirement ${index + 1} id`}
                value={req.id}
                onChange={(event) => {
                  const next = [...requirements];
                  next[index] = { ...next[index], id: event.target.value };
                  onChange({ ...value, requirements: next });
                }}
                className="w-20 shrink-0 rounded-md border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground outline-none"
              />
              <input
                aria-label={`Requirement ${index + 1} text`}
                value={req.text}
                onChange={(event) => {
                  const next = [...requirements];
                  next[index] = { ...next[index], text: event.target.value };
                  onChange({ ...value, requirements: next });
                }}
                className="flex-1 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground outline-none"
              />
              <button
                type="button"
                aria-label={`Remove requirement ${index + 1}`}
                onClick={() =>
                  onChange({
                    ...value,
                    requirements: requirements.filter((_, i) => i !== index),
                  })
                }
                className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                <Trash2 className="h-4 w-4" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-fit"
          data-testid="guided-add-requirement"
          onClick={() =>
            onChange({
              ...value,
              requirements: [
                ...requirements,
                { id: `R${requirements.length + 1}`, text: "" },
              ],
            })
          }
        >
          <Plus className="h-4 w-4" aria-hidden />
          Add requirement
        </Button>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
          Constraints
        </h3>
        <ul className="flex flex-col gap-2" data-testid="guided-constraints">
          {constraints.map((constraint, index) => (
            <li key={index} className="flex items-center gap-2">
              <input
                aria-label={`Constraint ${index + 1}`}
                value={constraint}
                onChange={(event) => {
                  const next = [...constraints];
                  next[index] = event.target.value;
                  onChange({ ...value, constraints: next });
                }}
                className="flex-1 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground outline-none"
              />
              <button
                type="button"
                aria-label={`Remove constraint ${index + 1}`}
                onClick={() =>
                  onChange({ ...value, constraints: constraints.filter((_, i) => i !== index) })
                }
                className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                <Trash2 className="h-4 w-4" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-fit"
          data-testid="guided-add-constraint"
          onClick={() => onChange({ ...value, constraints: [...constraints, ""] })}
        >
          <Plus className="h-4 w-4" aria-hidden />
          Add constraint
        </Button>
      </section>

      {saveError ? (
        <p role="alert" className="text-xs text-danger" data-testid="guided-save-error">
          {saveError}
        </p>
      ) : null}
    </div>
  );
}
