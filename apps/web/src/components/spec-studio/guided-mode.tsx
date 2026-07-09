"use client";

import { ChevronDown, ChevronRight, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  SPEC_STATUSES,
  type ADR,
  type AcceptanceCriterion,
  type ExecutionMode,
  type Requirement,
  type SpecManifest,
  type SpecStatus,
} from "@/lib/api/types";

import {
  addAcceptanceCriterion,
  addAdr,
  addRequirement,
  classifyCriterionStyle,
  composeChecklist,
  composeGivenWhenThen,
  computeChecklist,
  computeCoverage,
  computeNudges,
  convertCriterionText,
  type CriterionStyle,
  parseChecklist,
  parseGivenWhenThen,
} from "./guided-helpers";

export interface GuidedModeProps {
  /** The current draft manifest (controlled). */
  value: SpecManifest;
  onChange: (next: SpecManifest) => void;
  onSave: () => void;
  saving?: boolean;
  dirty?: boolean;
  saveError?: string | null;
}

const EXECUTION_MODES: { value: ExecutionMode; label: string }[] = [
  { value: "single_agent", label: "Single agent" },
  { value: "supervised_multi_agent", label: "Supervised swarm" },
];

/**
 * The Guided mode — a friendly, structured form over the same `SpecManifest`
 * the Markdown and YAML modes edit. Blocks: **Goal**, **Requirements**,
 * **Acceptance Criteria** (Given/When/Then, linked to requirements via a
 * dropdown — never by typing `(R#)`), **Constraints**, and a collapsed
 * **Advanced** section (constitution refs, execution mode, repos, ADRs).
 * Validation is surfaced as non-blocking nudges, alongside a Ready-to-create
 * checklist and a requirement-coverage meter.
 */
const CRITERION_STYLES: { value: CriterionStyle; label: string }[] = [
  { value: "gherkin", label: "Given/When/Then" },
  { value: "assertion", label: "Plain assertion" },
  { value: "checklist", label: "Checklist" },
];

export function GuidedMode({ value, onChange, onSave, saving = false, dirty = false, saveError }: GuidedModeProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Style is derived from each criterion's text, but an explicit pick (keyed by
  // criterion id) wins so an empty "assertion" doesn't snap back to the Gherkin
  // default. Editing the text never clears the pick — R# links are unaffected.
  const [styleOverrides, setStyleOverrides] = useState<Record<string, CriterionStyle>>({});
  const requirements = value.requirements ?? [];
  const criteria = value.acceptance_criteria ?? [];
  const constraints = value.constraints ?? [];
  const constitutionRefs = value.constitution_refs ?? [];
  const repos = value.repos ?? [];
  const decisions = value.decisions ?? [];

  const nudges = computeNudges(value);
  const checklist = computeChecklist(value);
  const coverage = computeCoverage(value);

  function setRequirements(next: Requirement[]) {
    onChange({ ...value, requirements: next });
  }

  function setCriteria(next: AcceptanceCriterion[]) {
    onChange({ ...value, acceptance_criteria: next });
  }

  function setDecisions(next: ADR[]) {
    onChange({ ...value, decisions: next });
  }

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
        <span className="font-medium text-foreground">Goal</span>
        <input
          data-testid="guided-name"
          value={value.name}
          onChange={(event) => onChange({ ...value, name: event.target.value })}
          placeholder="What does this spec achieve?"
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

      {/* --- Requirements ------------------------------------------------- */}
      <section className="flex flex-col gap-2">
        <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
          Requirements
        </h3>
        <ul className="flex flex-col gap-2" data-testid="guided-requirements">
          {requirements.map((req, index) => (
            <li key={req.id} className="flex items-center gap-2">
              <span
                data-testid={`requirement-id-${index}`}
                className="w-12 shrink-0 rounded-md border border-border bg-muted px-2 py-1.5 text-center font-mono text-xs text-muted-foreground"
              >
                {req.id}
              </span>
              <input
                aria-label={`${req.id} text`}
                value={req.text}
                onChange={(event) => {
                  const next = [...requirements];
                  next[index] = { ...next[index], text: event.target.value };
                  setRequirements(next);
                }}
                className="flex-1 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground outline-none"
              />
              <button
                type="button"
                aria-label={`Remove ${req.id}`}
                onClick={() => setRequirements(requirements.filter((_, i) => i !== index))}
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
          onClick={() => setRequirements(addRequirement(requirements))}
        >
          <Plus className="h-4 w-4" aria-hidden />
          Add requirement
        </Button>
      </section>

      {/* --- Acceptance Criteria ------------------------------------------ */}
      <section className="flex flex-col gap-2">
        <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
          Acceptance Criteria
        </h3>
        <ul className="flex flex-col gap-3" data-testid="guided-acceptance-criteria">
          {criteria.map((ac, index) => {
            const refs = ac.req_refs ?? [];
            const linkable = requirements.filter((r) => !refs.includes(r.id));
            const style = styleOverrides[ac.id] ?? classifyCriterionStyle(ac.text);

            function setText(text: string) {
              const next = [...criteria];
              next[index] = { ...next[index], text };
              setCriteria(next);
            }

            function changeStyle(nextStyle: CriterionStyle) {
              setStyleOverrides((prev) => ({ ...prev, [ac.id]: nextStyle }));
              setText(convertCriterionText(ac.text, nextStyle));
            }

            return (
              <li
                key={ac.id}
                className="flex flex-col gap-2 rounded-md border border-border bg-card/60 p-3"
                data-testid={`ac-item-${index}`}
              >
                <div className="flex items-center gap-2">
                  <span
                    data-testid={`acceptance-criterion-id-${index}`}
                    className="w-12 shrink-0 rounded-md border border-border bg-muted px-2 py-1.5 text-center font-mono text-xs text-muted-foreground"
                  >
                    {ac.id}
                  </span>
                  <select
                    aria-label={`${ac.id} style`}
                    data-testid={`ac-style-${index}`}
                    value={style}
                    onChange={(event) => changeStyle(event.target.value as CriterionStyle)}
                    className="rounded-md border border-border bg-card px-2 py-1 text-xs text-muted-foreground outline-none"
                  >
                    {CRITERION_STYLES.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    aria-label={`Remove ${ac.id}`}
                    onClick={() => setCriteria(criteria.filter((_, i) => i !== index))}
                    className="ml-auto rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                  >
                    <Trash2 className="h-4 w-4" aria-hidden />
                  </button>
                </div>

                {style === "gherkin" ? (
                  <GherkinEditor id={ac.id} text={ac.text} onChange={setText} />
                ) : style === "checklist" ? (
                  <ChecklistEditor id={ac.id} text={ac.text} onChange={setText} />
                ) : (
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="text-muted-foreground">Assertion</span>
                    <input
                      aria-label={`${ac.id} assertion`}
                      value={ac.text}
                      onChange={(event) => setText(event.target.value)}
                      placeholder="The system does X"
                      className="rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground outline-none"
                    />
                  </label>
                )}

                <div className="flex flex-wrap items-center gap-1.5">
                  {refs.map((refId) => (
                    <span
                      key={refId}
                      data-testid={`ac-linked-req-${index}-${refId}`}
                      className="inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-foreground"
                    >
                      {refId}
                      <button
                        type="button"
                        aria-label={`Unlink ${refId} from ${ac.id}`}
                        onClick={() => {
                          const next = [...criteria];
                          next[index] = { ...next[index], req_refs: refs.filter((r) => r !== refId) };
                          setCriteria(next);
                        }}
                        className="text-muted-foreground hover:text-foreground"
                      >
                        <Trash2 className="h-3 w-3" aria-hidden />
                      </button>
                    </span>
                  ))}
                  {linkable.length > 0 ? (
                    <select
                      aria-label={`Link a requirement to ${ac.id}`}
                      data-testid={`ac-link-requirement-${index}`}
                      value=""
                      onChange={(event) => {
                        const reqId = event.target.value;
                        if (!reqId) return;
                        const next = [...criteria];
                        next[index] = { ...next[index], req_refs: [...refs, reqId] };
                        setCriteria(next);
                      }}
                      className="rounded-md border border-dashed border-border bg-transparent px-2 py-0.5 text-[11px] text-muted-foreground outline-none"
                    >
                      <option value="">Link requirement…</option>
                      {linkable.map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.id} — {r.text || "untitled"}
                        </option>
                      ))}
                    </select>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-fit"
          data-testid="guided-add-acceptance-criterion"
          onClick={() => setCriteria(addAcceptanceCriterion(criteria))}
        >
          <Plus className="h-4 w-4" aria-hidden />
          Add acceptance criterion
        </Button>
      </section>

      {/* --- Constraints ---------------------------------------------------- */}
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

      {/* --- Advanced (collapsed by default) -------------------------------- */}
      <section className="flex flex-col gap-2 rounded-md border border-border">
        <button
          type="button"
          data-testid="guided-advanced-toggle"
          aria-expanded={advancedOpen}
          onClick={() => setAdvancedOpen((open) => !open)}
          className="flex items-center gap-1.5 px-3 py-2 text-left text-sm font-medium text-foreground"
        >
          {advancedOpen ? (
            <ChevronDown className="h-4 w-4" aria-hidden />
          ) : (
            <ChevronRight className="h-4 w-4" aria-hidden />
          )}
          Advanced
        </button>
        {advancedOpen ? (
          <div className="flex flex-col gap-4 border-t border-border px-3 py-3" data-testid="guided-advanced-panel">
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium text-foreground">Execution mode</span>
              <select
                data-testid="guided-execution-mode"
                value={value.execution_mode ?? ""}
                onChange={(event) =>
                  onChange({
                    ...value,
                    execution_mode: (event.target.value || undefined) as ExecutionMode | undefined,
                  })
                }
                className="rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none"
              >
                <option value="">Default</option>
                {EXECUTION_MODES.map((mode) => (
                  <option key={mode.value} value={mode.value}>
                    {mode.label}
                  </option>
                ))}
              </select>
            </label>

            <StringListField
              label="Constitution refs"
              testId="guided-constitution-refs"
              addTestId="guided-add-constitution-ref"
              items={constitutionRefs}
              onChange={(next) => onChange({ ...value, constitution_refs: next })}
            />

            <StringListField
              label="Repos"
              testId="guided-repos"
              addTestId="guided-add-repo"
              items={repos}
              onChange={(next) => onChange({ ...value, repos: next })}
            />

            <div className="flex flex-col gap-2">
              <span className="text-sm font-medium text-foreground">Architecture decisions</span>
              <ul className="flex flex-col gap-2" data-testid="guided-decisions">
                {decisions.map((adr, index) => (
                  <li key={adr.id} className="flex items-center gap-2">
                    <span className="w-14 shrink-0 rounded-md border border-border bg-muted px-2 py-1.5 text-center font-mono text-xs text-muted-foreground">
                      {adr.id}
                    </span>
                    <input
                      aria-label={`${adr.id} title`}
                      value={adr.title}
                      onChange={(event) => {
                        const next = [...decisions];
                        next[index] = { ...next[index], title: event.target.value };
                        setDecisions(next);
                      }}
                      className="flex-1 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground outline-none"
                      placeholder="Decision title"
                    />
                    <button
                      type="button"
                      aria-label={`Remove ${adr.id}`}
                      onClick={() => setDecisions(decisions.filter((_, i) => i !== index))}
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
                data-testid="guided-add-decision"
                onClick={() => setDecisions(addAdr(decisions))}
              >
                <Plus className="h-4 w-4" aria-hidden />
                Add decision
              </Button>
            </div>
          </div>
        ) : null}
      </section>

      {/* --- Nudges ---------------------------------------------------------- */}
      {nudges.length > 0 ? (
        <ul className="flex flex-col gap-1.5" data-testid="guided-nudges" aria-label="Validation nudges">
          {nudges.map((nudge) => (
            <li
              key={nudge.id}
              data-testid={`guided-nudge-${nudge.id}`}
              className="rounded-md border border-dashed border-warning/40 bg-warning/10 px-3 py-1.5 text-xs text-warning-foreground"
            >
              {nudge.message}
            </li>
          ))}
        </ul>
      ) : null}

      {/* --- Ready-to-create checklist + coverage meter ---------------------- */}
      <section
        className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-3"
        data-testid="guided-checklist"
      >
        <div className="flex items-center justify-between gap-3">
          <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
            Ready to create
          </h3>
          <span className="font-mono text-xs text-muted-foreground" data-testid="guided-coverage-meter">
            {coverage.satisfied}/{coverage.total} requirements covered ({coverage.pct}%)
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div
            className={`h-full rounded-full ${coverage.pct >= 100 ? "bg-success" : "bg-primary"}`}
            style={{ width: `${coverage.pct}%` }}
          />
        </div>
        <ul className="flex flex-col gap-1">
          {checklist.map((item) => (
            <li
              key={item.id}
              data-testid={`checklist-item-${item.id}`}
              className="flex items-center gap-2 text-xs"
            >
              <span
                aria-hidden
                className={`h-3.5 w-3.5 shrink-0 rounded-full border ${
                  item.done ? "border-success bg-success" : "border-border bg-transparent"
                }`}
              />
              <span className={item.done ? "text-foreground" : "text-muted-foreground"}>
                {item.label}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {saveError ? (
        <p role="alert" className="text-xs text-danger" data-testid="guided-save-error">
          {saveError}
        </p>
      ) : null}
    </div>
  );
}

function StringListField({
  label,
  testId,
  addTestId,
  items,
  onChange,
}: {
  label: string;
  testId: string;
  addTestId: string;
  items: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium text-foreground">{label}</span>
      <ul className="flex flex-col gap-2" data-testid={testId}>
        {items.map((item, index) => (
          <li key={index} className="flex items-center gap-2">
            <input
              aria-label={`${label} ${index + 1}`}
              value={item}
              onChange={(event) => {
                const next = [...items];
                next[index] = event.target.value;
                onChange(next);
              }}
              className="flex-1 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground outline-none"
            />
            <button
              type="button"
              aria-label={`Remove ${label} ${index + 1}`}
              onClick={() => onChange(items.filter((_, i) => i !== index))}
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
        data-testid={addTestId}
        onClick={() => onChange([...items, ""])}
      >
        <Plus className="h-4 w-4" aria-hidden />
        Add
      </Button>
    </div>
  );
}

/** Given/When/Then editor — three inputs composing the criterion's `text`. */
function GherkinEditor({ id, text, onChange }: { id: string; text: string; onChange: (text: string) => void }) {
  const gwt = parseGivenWhenThen(text);
  const update = (patch: Partial<typeof gwt>) => onChange(composeGivenWhenThen({ ...gwt, ...patch }));
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
      {(["given", "when", "then"] as const).map((clause) => (
        <label key={clause} className="flex flex-col gap-1 text-xs">
          <span className="capitalize text-muted-foreground">{clause}</span>
          <input
            aria-label={`${id} ${clause}`}
            value={gwt[clause]}
            onChange={(event) => update({ [clause]: event.target.value })}
            className="rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground outline-none"
          />
        </label>
      ))}
    </div>
  );
}

/** Checklist editor — a togglable, editable list of check items in `text`. */
function ChecklistEditor({ id, text, onChange }: { id: string; text: string; onChange: (text: string) => void }) {
  const items = parseChecklist(text);
  const commit = (next: typeof items) => onChange(composeChecklist(next));
  return (
    <div className="flex flex-col gap-1.5" data-testid={`ac-checklist-${id}`}>
      <ul className="flex flex-col gap-1.5">
        {items.map((item, index) => (
          <li key={index} className="flex items-center gap-2">
            <input
              type="checkbox"
              aria-label={`${id} item ${index + 1} done`}
              checked={item.checked}
              onChange={(event) =>
                commit(items.map((it, i) => (i === index ? { ...it, checked: event.target.checked } : it)))
              }
              className="h-4 w-4 shrink-0 accent-primary"
            />
            <input
              aria-label={`${id} item ${index + 1}`}
              value={item.label}
              onChange={(event) =>
                commit(items.map((it, i) => (i === index ? { ...it, label: event.target.value } : it)))
              }
              placeholder="Checklist item"
              className="flex-1 rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground outline-none"
            />
            <button
              type="button"
              aria-label={`Remove ${id} item ${index + 1}`}
              onClick={() => commit(items.filter((_, i) => i !== index))}
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
        data-testid={`ac-checklist-add-${id}`}
        onClick={() => commit([...items, { label: "", checked: false }])}
      >
        <Plus className="h-4 w-4" aria-hidden />
        Add item
      </Button>
    </div>
  );
}
