"use client";

import { AlertTriangle, CheckCircle2, ListTree, Eye as EyeIcon, Route } from "lucide-react";
import { useMemo, useRef, useState, type KeyboardEvent, type ReactNode, type UIEvent } from "react";

import { Button } from "@/components/ui/button";
import { TraceabilityMatrix } from "@/components/spec/traceability-matrix";
import { computeChecklist, computeCoverage, computeNudges } from "@/components/spec-studio/guided-helpers";
import { hasMarkdownErrors, parseSpecMarkdown, type MarkdownIssue } from "@/lib/spec-studio/markdown-parse";
import type { RequirementTrace } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface MarkdownModeProps {
  /** The current `spec.md` text (controlled). */
  value: string;
  onChange: (next: string) => void;
  onSave: () => void;
  saving?: boolean;
  dirty?: boolean;
  saveError?: string | null;
  /** Live-collaboration presence bar rendered in the header (CRDT mode). */
  presence?: ReactNode;
  /** Report the local cursor/selection for remote presence (CRDT mode). */
  onSelectionChange?: (anchor: number, head: number) => void;
}

type PanelTab = "structure" | "preview" | "traceability";

const PANEL_TABS: { id: PanelTab; label: string; icon: typeof ListTree }[] = [
  { id: "structure", label: "Structure", icon: ListTree },
  { id: "preview", label: "Preview", icon: EyeIcon },
  { id: "traceability", label: "Traceability", icon: Route },
];

function jumpToLine(textareaRef: React.RefObject<HTMLTextAreaElement | null>, line: number) {
  const textarea = textareaRef.current;
  if (!textarea) return;
  const lines = textarea.value.split("\n");
  let offset = 0;
  for (let i = 0; i < line - 1 && i < lines.length; i += 1) {
    offset += lines[i].length + 1;
  }
  textarea.focus();
  textarea.setSelectionRange(offset, offset);
}

/** Build a lightweight, local requirement traceability from the parsed markdown alone (no task/test refs — those come from a backend validation run). */
function localTraces(
  manifest: ReturnType<typeof parseSpecMarkdown>["manifest"],
): RequirementTrace[] {
  const requirements = manifest.requirements ?? [];
  const criteria = manifest.acceptance_criteria ?? [];
  return requirements.map((req) => {
    const linked = criteria.filter((ac) => (ac.req_refs ?? []).includes(req.id)).map((ac) => ac.id);
    return {
      requirement_id: req.id,
      text: req.text,
      acceptance_criteria_ids: linked,
      task_refs: [],
      test_refs: [],
      satisfied: linked.length > 0,
    };
  });
}

function IssueList({
  issues,
  onJump,
}: {
  issues: MarkdownIssue[];
  onJump: (line: number) => void;
}) {
  if (issues.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1" data-testid="markdown-issues" aria-label="spec.md parse issues">
      {issues.map((issue, index) => (
        <li key={`${issue.line}-${index}`}>
          <button
            type="button"
            className={cn(
              "flex w-full items-start gap-2 rounded-md border px-3 py-1.5 text-left text-xs",
              issue.severity === "error"
                ? "border-danger/30 bg-danger/5 text-danger"
                : "border-warning/30 bg-warning/5 text-warning",
            )}
            onClick={() => onJump(issue.line)}
          >
            <span className="font-mono text-[11px] shrink-0">Ln {issue.line}</span>
            <span>{issue.message}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function StructurePanel({ manifest }: { manifest: ReturnType<typeof parseSpecMarkdown>["manifest"] }) {
  const checklist = computeChecklist(manifest);
  const nudges = computeNudges(manifest);
  const coverage = computeCoverage(manifest);
  const counts: { label: string; count: number }[] = [
    { label: "Requirements", count: (manifest.requirements ?? []).length },
    { label: "Acceptance criteria", count: (manifest.acceptance_criteria ?? []).length },
    { label: "Constraints", count: (manifest.constraints ?? []).length },
    { label: "Open questions", count: (manifest.open_questions ?? []).length },
    { label: "Decisions", count: (manifest.decisions ?? []).length },
  ];
  return (
    <div className="flex flex-col gap-4" data-testid="markdown-panel-structure">
      <dl className="grid grid-cols-2 gap-3">
        {counts.map((c) => (
          <div key={c.label} className="rounded-md border border-border bg-card/60 px-3 py-2">
            <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{c.label}</dt>
            <dd className="font-mono text-lg text-foreground">{c.count}</dd>
          </div>
        ))}
      </dl>
      <div>
        <p className="mb-1 text-xs font-medium text-foreground">
          Requirement coverage: {coverage.satisfied}/{coverage.total} ({coverage.pct}%)
        </p>
        <ul className="flex flex-col gap-1" data-testid="markdown-checklist">
          {checklist.map((item) => (
            <li key={item.id} className="flex items-center gap-2 text-xs">
              <CheckCircle2
                className={cn("h-3.5 w-3.5 shrink-0", item.done ? "text-success" : "text-muted-foreground/40")}
                aria-hidden
              />
              <span className={item.done ? "text-foreground" : "text-muted-foreground"}>{item.label}</span>
            </li>
          ))}
        </ul>
      </div>
      {nudges.length > 0 ? (
        <ul className="flex flex-col gap-1" data-testid="markdown-nudges">
          {nudges.map((nudge) => (
            <li
              key={nudge.id}
              className="flex items-start gap-1.5 rounded-md border border-warning/30 bg-warning/5 px-2 py-1 text-xs text-warning"
            >
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              {nudge.message}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function PreviewPanel({ manifest }: { manifest: ReturnType<typeof parseSpecMarkdown>["manifest"] }) {
  const requirements = manifest.requirements ?? [];
  const criteria = manifest.acceptance_criteria ?? [];
  const constraints = manifest.constraints ?? [];
  const openQuestions = manifest.open_questions ?? [];
  const decisions = manifest.decisions ?? [];
  return (
    <div className="flex flex-col gap-5" data-testid="markdown-panel-preview">
      <section>
        <h3 className="font-display text-sm font-semibold text-foreground">Goal</h3>
        <p className="text-sm text-foreground/90">{manifest.name || "—"}</p>
      </section>
      {requirements.length > 0 ? (
        <section>
          <h3 className="font-display text-sm font-semibold text-foreground">Requirements</h3>
          <ul className="flex flex-col gap-1">
            {requirements.map((r) => (
              <li key={r.id} className="text-sm text-foreground/90">
                <span className="font-mono text-xs text-primary">{r.id}</span> {r.text}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {criteria.length > 0 ? (
        <section>
          <h3 className="font-display text-sm font-semibold text-foreground">Acceptance Criteria</h3>
          <ul className="flex flex-col gap-1">
            {criteria.map((c) => (
              <li key={c.id} className="text-sm text-foreground/90">
                <span className="font-mono text-xs text-primary">{c.id}</span>{" "}
                {(c.req_refs ?? []).length > 0 ? (
                  <span className="font-mono text-[11px] text-muted-foreground">
                    ({(c.req_refs ?? []).join(", ")})
                  </span>
                ) : null}{" "}
                {c.text}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {constraints.length > 0 ? (
        <section>
          <h3 className="font-display text-sm font-semibold text-foreground">Constraints</h3>
          <ul className="flex flex-col gap-1">
            {constraints.map((c, i) => (
              <li key={i} className="text-sm text-foreground/90">
                {c}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {openQuestions.length > 0 ? (
        <section>
          <h3 className="font-display text-sm font-semibold text-foreground">Open Questions</h3>
          <ul className="flex flex-col gap-1">
            {openQuestions.map((q) => (
              <li key={q.id} className="text-sm text-foreground/90">
                <span className="font-mono text-xs text-primary">{q.id}</span> {q.text}
                {q.resolution ? (
                  <span className="block pl-4 text-xs text-success">Resolution: {q.resolution}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {decisions.length > 0 ? (
        <section>
          <h3 className="font-display text-sm font-semibold text-foreground">Decisions</h3>
          <ul className="flex flex-col gap-2">
            {decisions.map((d) => (
              <li key={d.id} className="text-sm text-foreground/90">
                <span className="font-mono text-xs text-primary">{d.id}</span> — {d.title}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/**
 * The `spec.md` prose editor — Spec Studio's default human/agent surface.
 * A JetBrains Mono, keyboard-first raw-text editor (frontmatter always
 * visible, `Tab` inserts an indent instead of leaving the field, a
 * line-number gutter) paired with a live parsed pane — **Structure**
 * (section counts, the Ready checklist and coverage meter),
 * **Preview** (a readable render of the parsed sections) and
 * **Traceability** (local requirement -> acceptance-criteria coverage) — plus
 * a line-anchored parse-issue list, all recomputed on every keystroke via
 * `parseSpecMarkdown` (a client-side mirror of `forge_spec.markdown.parse_spec_md`).
 * Saving re-renders `manifest.yaml` to match on the backend, which remains
 * the authoritative parser.
 */
export function MarkdownMode({
  value,
  onChange,
  onSave,
  saving = false,
  dirty = false,
  saveError,
  presence,
  onSelectionChange,
}: MarkdownModeProps) {
  const [panel, setPanel] = useState<PanelTab>("structure");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  const { manifest, issues } = useMemo(() => parseSpecMarkdown(value), [value]);
  const invalid = hasMarkdownErrors(issues);
  const lineCount = useMemo(() => Math.max(1, value.split("\n").length), [value]);
  const traces = useMemo(() => localTraces(manifest), [manifest]);

  const onScroll = (event: UIEvent<HTMLTextAreaElement>) => {
    setScrollTop(event.currentTarget.scrollTop);
  };

  /** Keyboard-first: `Tab` indents (two spaces) instead of leaving the editor. */
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const { selectionStart, selectionEnd } = textarea;
    const next = `${value.slice(0, selectionStart)}  ${value.slice(selectionEnd)}`;
    onChange(next);
    requestAnimationFrame(() => {
      textarea.setSelectionRange(selectionStart + 2, selectionStart + 2);
    });
  };

  return (
    <div className="flex flex-col gap-3" data-testid="markdown-mode">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {invalid ? (
            <span className="inline-flex items-center gap-1 text-danger" data-testid="markdown-status-invalid">
              <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
              {issues.filter((i) => i.severity === "error").length} issue
              {issues.filter((i) => i.severity === "error").length === 1 ? "" : "s"}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-success" data-testid="markdown-status-valid">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
              Parses cleanly
            </span>
          )}
          {dirty ? <span className="text-muted-foreground/70">Unsaved changes</span> : null}
          {presence}
        </div>
        <Button size="sm" onClick={onSave} disabled={saving || !dirty} data-testid="markdown-save">
          {saving ? "Saving…" : "Save spec.md"}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="flex overflow-hidden rounded-lg border border-border bg-card">
          <div
            ref={gutterRef}
            aria-hidden
            className="select-none overflow-hidden border-r border-border bg-muted/40 px-3 py-3 text-right font-mono text-xs leading-5 text-muted-foreground/70"
            style={{ transform: `translateY(-${scrollTop}px)` }}
          >
            {Array.from({ length: lineCount }, (_, i) => (
              <div key={i}>{i + 1}</div>
            ))}
          </div>
          <textarea
            ref={textareaRef}
            data-testid="markdown-textarea"
            aria-label="spec.md"
            spellCheck={false}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onScroll={onScroll}
            onKeyDown={onKeyDown}
            onSelect={(event) =>
              onSelectionChange?.(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
            }
            className="min-h-[28rem] flex-1 resize-none bg-transparent px-3 py-3 font-mono text-xs leading-5 text-foreground outline-none"
          />
        </div>

        <div className="flex flex-col gap-3 rounded-lg border border-border bg-card/60 p-3">
          <div
            role="tablist"
            aria-label="spec.md parsed view"
            className="inline-flex w-fit items-center gap-1 rounded-lg border border-border bg-muted/50 p-1"
          >
            {PANEL_TABS.map((tab) => {
              const Icon = tab.icon;
              const selected = tab.id === panel;
              return (
                <button
                  key={tab.id}
                  role="tab"
                  type="button"
                  aria-selected={selected}
                  onClick={() => setPanel(tab.id)}
                  data-testid={`markdown-panel-tab-${tab.id}`}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    selected ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <Icon className="h-3.5 w-3.5" aria-hidden />
                  {tab.label}
                </button>
              );
            })}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {panel === "structure" ? <StructurePanel manifest={manifest} /> : null}
            {panel === "preview" ? <PreviewPanel manifest={manifest} /> : null}
            {panel === "traceability" ? (
              <div data-testid="markdown-panel-traceability">
                <TraceabilityMatrix traces={traces} />
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {saveError ? (
        <p role="alert" className="text-xs text-danger" data-testid="markdown-save-error">
          {saveError}
        </p>
      ) : null}

      <IssueList issues={issues} onJump={(line) => jumpToLine(textareaRef, line)} />
    </div>
  );
}
