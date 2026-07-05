"use client";

import { MousePointerSquareDashed, Trash2 } from "lucide-react";
import { useId } from "react";

import type {
  WorkflowCatalog,
  WorkflowGraph,
  WorkflowStateNode,
  WorkflowTransitionEdge,
  WorkflowValidationIssue,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { IssueRow } from "./validation-panel";
import type { Selection } from "./selection";
import {
  edgeTrigger,
  issuesForEdge,
  issuesForNode,
  nodeKindMeta,
  parseWhenInput,
  whenToText,
} from "./workflow-graph";

const FIELD =
  "w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";

const NONE = "— none —";

export interface WorkflowInspectorProps {
  graph: WorkflowGraph;
  selection: Selection | null;
  catalog?: WorkflowCatalog;
  editable: boolean;
  issues: WorkflowValidationIssue[];
  onUpdateNode: (id: string, patch: Partial<WorkflowStateNode>) => void;
  onUpdateEdge: (id: string, patch: Partial<WorkflowTransitionEdge>) => void;
  onRemoveEdge: (id: string) => void;
  onAddTransitionFrom: (fromId: string) => void;
}

/** Right-rail inspector: read + edit the selected state or transition. */
export function WorkflowInspector(props: WorkflowInspectorProps) {
  const { graph, selection } = props;

  if (!selection) {
    return (
      <div
        data-testid="inspector-empty"
        className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center"
      >
        <MousePointerSquareDashed
          className="h-7 w-7 text-muted-foreground"
          aria-hidden
        />
        <p className="text-sm font-medium text-foreground">Nothing selected</p>
        <p className="max-w-[16rem] text-xs text-muted-foreground">
          Pick a state or a transition on the canvas to inspect and edit it.
          Press Enter on a focused node to select it.
        </p>
      </div>
    );
  }

  if (selection.kind === "node") {
    const node = graph.nodes.find((n) => n.id === selection.id);
    if (!node) return null;
    return <NodeInspector {...props} node={node} />;
  }

  const edge = graph.edges.find((e) => e.id === selection.id);
  if (!edge) return null;
  return <EdgeInspector {...props} edge={edge} />;
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </span>
      {children}
    </div>
  );
}

function NodeInspector({
  graph,
  editable,
  issues,
  node,
  onUpdateNode,
  onAddTransitionFrom,
}: WorkflowInspectorProps & { node: WorkflowStateNode }) {
  const labelId = useId();
  const kind = nodeKindMeta(node.kind);
  const outgoing = graph.edges.filter((e) => e.from_state === node.id).length;
  const incoming = graph.edges.filter((e) => e.to_state === node.id).length;
  const nodeIssues = issuesForNode(issues, node.id);

  return (
    <div
      data-testid="node-inspector"
      className="flex h-full flex-col gap-4 overflow-y-auto p-4"
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={cn("h-2.5 w-2.5 rounded-full", kind.dot)} aria-hidden />
          <h3 className="font-mono text-sm font-semibold text-foreground">
            {node.id}
          </h3>
        </div>
        <span className={cn("text-[11px] font-medium", kind.accent)}>
          {kind.label}
        </span>
      </header>

      <Section title="Display label">
        <input
          id={labelId}
          className={FIELD}
          value={node.label ?? ""}
          placeholder={node.id}
          disabled={!editable}
          aria-label="State display label"
          onChange={(e) =>
            onUpdateNode(node.id, { label: e.target.value || null })
          }
        />
      </Section>

      <div className="grid grid-cols-2 gap-2">
        <Stat label="Outgoing" value={outgoing} />
        <Stat label="Incoming" value={incoming} />
      </div>

      {editable ? (
        <button
          type="button"
          data-testid="add-transition-from"
          onClick={() => onAddTransitionFrom(node.id)}
          className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Add transition from here
        </button>
      ) : null}

      {nodeIssues.length > 0 ? (
        <Section title={`Issues (${nodeIssues.length})`}>
          <ul className="flex flex-col gap-1.5">
            {nodeIssues.map((issue, i) => (
              <IssueRow key={i} issue={issue} />
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

function EdgeInspector({
  graph,
  catalog,
  editable,
  issues,
  edge,
  onUpdateEdge,
  onRemoveEdge,
}: WorkflowInspectorProps & { edge: WorkflowTransitionEdge }) {
  const fromId = useId();
  const toId = useId();
  const actionId = useId();
  const whenId = useId();
  const eventsListId = useId();
  const conditionId = useId();
  const skillId = useId();

  const stateOptions = graph.nodes.map((n) => n.id);
  const effects = catalog?.effects ?? [];
  const guards = catalog?.guards ?? [];
  const preconditions = catalog?.preconditions ?? [];
  const skills = catalog?.skills ?? [];
  const events = catalog?.events ?? [];
  const edgeIssues = issuesForEdge(issues, edge.id);

  function togglePrecondition(name: string, on: boolean) {
    const next = on
      ? [...edge.preconditions, name]
      : edge.preconditions.filter((p) => p !== name);
    onUpdateEdge(edge.id, { preconditions: next });
  }

  return (
    <div
      data-testid="edge-inspector"
      className="flex h-full flex-col gap-4 overflow-y-auto p-4"
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 font-mono text-sm font-semibold text-foreground">
          <span>{edge.from_state}</span>
          <span className="text-muted-foreground">&rarr;</span>
          <span>{edge.to_state}</span>
        </h3>
        <span className="rounded-full border border-border px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
          {edgeTrigger(edge)}
        </span>
      </header>

      <div className="grid grid-cols-2 gap-2">
        <Section title="From">
          <select
            id={fromId}
            className={FIELD}
            value={edge.from_state}
            disabled={!editable}
            aria-label="Transition from state"
            onChange={(e) => onUpdateEdge(edge.id, { from_state: e.target.value })}
          >
            {stateOptions.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </Section>
        <Section title="To">
          <select
            id={toId}
            className={FIELD}
            value={edge.to_state}
            disabled={!editable}
            aria-label="Transition to state"
            onChange={(e) => onUpdateEdge(edge.id, { to_state: e.target.value })}
          >
            {stateOptions.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </Section>
      </div>

      <Section title="Effect (action)">
        <select
          id={actionId}
          className={FIELD}
          value={edge.action ?? ""}
          disabled={!editable}
          aria-label="Transition effect"
          onChange={(e) =>
            onUpdateEdge(edge.id, { action: e.target.value || null })
          }
        >
          <option value="">{NONE}</option>
          {effects.map((effect) => (
            <option key={effect.name} value={effect.name}>
              {effect.name}
            </option>
          ))}
        </select>
      </Section>

      <Section title="On (event / signal)">
        <input
          id={whenId}
          className={cn(FIELD, "font-mono text-xs")}
          value={whenToText(edge.when)}
          list={eventsListId}
          placeholder="e.g. ci_status_green, review_approved_by_human"
          disabled={!editable}
          aria-label="Transition events"
          onChange={(e) =>
            onUpdateEdge(edge.id, { when: parseWhenInput(e.target.value) })
          }
        />
        <datalist id={eventsListId}>
          {events.map((event) => (
            <option key={event} value={event} />
          ))}
        </datalist>
      </Section>

      <Section title="Guard (condition)">
        <select
          id={conditionId}
          className={FIELD}
          value={edge.condition ?? ""}
          disabled={!editable}
          aria-label="Transition guard"
          onChange={(e) =>
            onUpdateEdge(edge.id, { condition: e.target.value || null })
          }
        >
          <option value="">{NONE}</option>
          {guards.map((guard) => (
            <option key={guard.name} value={guard.name}>
              {guard.name}
            </option>
          ))}
        </select>
      </Section>

      {preconditions.length > 0 ? (
        <Section title="Preconditions">
          <div className="flex flex-col gap-1.5">
            {preconditions.map((pre) => {
              const checked = edge.preconditions.includes(pre.name);
              return (
                <label
                  key={pre.name}
                  className="flex items-center gap-2 text-xs text-foreground"
                >
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 rounded border-input accent-primary"
                    checked={checked}
                    disabled={!editable}
                    onChange={(e) =>
                      togglePrecondition(pre.name, e.target.checked)
                    }
                  />
                  <span className="font-mono">{pre.name}</span>
                </label>
              );
            })}
          </div>
        </Section>
      ) : null}

      {skills.length > 0 ? (
        <Section title="Skill">
          <select
            id={skillId}
            className={FIELD}
            value={edge.skill ?? ""}
            disabled={!editable}
            aria-label="Transition skill"
            onChange={(e) =>
              onUpdateEdge(edge.id, { skill: e.target.value || null })
            }
          >
            <option value="">{NONE}</option>
            {skills.map((skill) => (
              <option key={skill} value={skill}>
                {skill}
              </option>
            ))}
          </select>
        </Section>
      ) : null}

      {edgeIssues.length > 0 ? (
        <Section title={`Issues (${edgeIssues.length})`}>
          <ul className="flex flex-col gap-1.5">
            {edgeIssues.map((issue, i) => (
              <IssueRow key={i} issue={issue} />
            ))}
          </ul>
        </Section>
      ) : null}

      {editable ? (
        <button
          type="button"
          data-testid="remove-transition"
          onClick={() => onRemoveEdge(edge.id)}
          className="mt-auto inline-flex h-9 items-center justify-center gap-2 rounded-md border border-danger/40 px-3 text-sm font-medium text-danger transition-colors hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Trash2 className="h-4 w-4" aria-hidden />
          Remove transition
        </button>
      ) : null}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border border-border bg-muted/40 px-2.5 py-1.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-sm text-foreground">{value}</span>
    </div>
  );
}
