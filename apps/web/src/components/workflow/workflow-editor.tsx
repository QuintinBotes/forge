"use client";

import {
  CheckCircle2,
  GitFork,
  Plus,
  Save,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useForkBundledWorkflow,
  usePublishWorkflow,
  useSaveWorkflowDraft,
  useValidateWorkflowDraft,
  useWorkflowCatalog,
  useWorkflowDefinition,
  useWorkflowDefinitions,
} from "@/lib/api/workflow";
import type {
  WorkflowDefinitionDetail,
  WorkflowGraph,
  WorkflowNodeKind,
  WorkflowStateNode,
  WorkflowTransitionEdge,
  WorkflowValidationIssue,
  WorkflowValidationState,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { DefinitionRail } from "./definition-rail";
import { NewWorkflowDialog } from "./new-workflow-dialog";
import type { Selection } from "./selection";
import { ValidationPanel } from "./validation-panel";
import { WorkflowCanvas } from "./workflow-canvas";
import {
  edgesWithErrors,
  errorCount,
  graphsEqual,
  nextEdgeId,
  nodesWithErrors,
} from "./workflow-graph";
import { WorkflowInspector } from "./workflow-inspector";

const TERMINAL_STATES = new Set(["closed", "failed", "cancelled"]);
const HUMAN_GATE_STATES = new Set([
  "spec_review",
  "plan_review",
  "awaiting_review",
  "needs_human_input",
  "awaiting_approval",
]);

function deriveKind(id: string): WorkflowNodeKind {
  if (TERMINAL_STATES.has(id)) return "terminal";
  if (HUMAN_GATE_STATES.has(id)) return "human_gate";
  return "normal";
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** The active revision to open: the editable draft, else the published one. */
function activeRevision(detail: WorkflowDefinitionDetail) {
  if (detail.editable && detail.draft) return detail.draft;
  return detail.current_published ?? detail.draft ?? null;
}

function loadKeyOf(detail: WorkflowDefinitionDetail): string {
  return [
    detail.name,
    detail.editable ? "e" : "r",
    detail.draft?.id ?? "",
    detail.current_published?.id ?? "",
  ].join("|");
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

export interface WorkflowEditorProps {
  client?: ForgeApiClient;
}

/**
 * Workflow visual editor (F28) — a governed, versioned authoring surface over
 * the workflow DSL. The left rail lists every definition; the center is a
 * hand-rolled SVG state-machine canvas; the right rail inspects and edits the
 * selected state or transition and surfaces server-authoritative validation.
 * Editing mutates a local working graph (optimistic, sub-100ms); Save draft
 * persists + re-validates it, Validate re-checks the saved draft, and Publish —
 * the single ember action — promotes it once it has zero errors. Bundled
 * definitions are read-only and fork into an editable copy.
 */
export function WorkflowEditor({ client = apiClient }: WorkflowEditorProps) {
  const definitionsQuery = useWorkflowDefinitions(client);
  const catalogQuery = useWorkflowCatalog(client);

  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [draftGraph, setDraftGraph] = useState<WorkflowGraph | null>(null);
  const [baselineGraph, setBaselineGraph] = useState<WorkflowGraph | null>(null);
  const [issues, setIssues] = useState<WorkflowValidationIssue[]>([]);
  const [validationState, setValidationState] =
    useState<WorkflowValidationState>("unvalidated");
  const [savedDraftExists, setSavedDraftExists] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);

  const definitions = useMemo(
    () => definitionsQuery.data ?? [],
    [definitionsQuery.data],
  );

  // Default selection: prefer an in-progress draft, else the first workflow.
  useEffect(() => {
    if (selectedName || definitions.length === 0) return;
    const withDraft = definitions.find((d) => d.has_draft);
    setSelectedName((withDraft ?? definitions[0]).name);
  }, [definitions, selectedName]);

  const detailQuery = useWorkflowDefinition(selectedName, client);
  const detail = detailQuery.data ?? null;
  const editable = detail?.editable ?? false;

  // Load the selected definition's graph into the working copy. Reseeds when the
  // definition identity changes (switch / fork / publish), never on a plain
  // background refetch — post-mutation state is applied in the callbacks below.
  const loadedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!detail) return;
    const key = loadKeyOf(detail);
    if (loadedRef.current === key) return;
    loadedRef.current = key;
    const active = activeRevision(detail);
    setDraftGraph(active ? clone(active.graph) : null);
    setBaselineGraph(active ? clone(active.graph) : null);
    setIssues(active?.validation_issues ?? []);
    setValidationState(active?.validation_status ?? "unvalidated");
    setSavedDraftExists(Boolean(detail.draft));
    setSelection(null);
    setActionError(null);
  }, [detail]);

  const save = useSaveWorkflowDraft(client);
  const validate = useValidateWorkflowDraft(client);
  const publish = usePublishWorkflow(client);
  const fork = useForkBundledWorkflow(client);

  const dirty = Boolean(
    draftGraph && baselineGraph && !graphsEqual(draftGraph, baselineGraph),
  );
  const errors = errorCount(issues);
  const errorNodeIds = useMemo(() => nodesWithErrors(issues), [issues]);
  const errorEdgeIds = useMemo(() => edgesWithErrors(issues), [issues]);

  const actionPending =
    save.isPending || validate.isPending || publish.isPending || fork.isPending;

  // --- selection ---------------------------------------------------------- //
  const selectNode = useCallback((id: string) => {
    setSelection({ kind: "node", id });
    setActionError(null);
  }, []);
  const selectEdge = useCallback((id: string) => {
    setSelection({ kind: "edge", id });
    setActionError(null);
  }, []);
  const clearSelection = useCallback(() => setSelection(null), []);

  const pickDefinition = useCallback((name: string) => {
    setSelectedName(name);
  }, []);

  // --- graph edits (local, optimistic) ------------------------------------ //
  const updateNode = useCallback(
    (id: string, patch: Partial<WorkflowStateNode>) => {
      setDraftGraph((prev) =>
        prev
          ? {
              ...prev,
              nodes: prev.nodes.map((node) =>
                node.id === id ? { ...node, ...patch } : node,
              ),
            }
          : prev,
      );
    },
    [],
  );

  const updateEdge = useCallback(
    (id: string, patch: Partial<WorkflowTransitionEdge>) => {
      setDraftGraph((prev) =>
        prev
          ? {
              ...prev,
              edges: prev.edges.map((edge) =>
                edge.id === id ? { ...edge, ...patch } : edge,
              ),
            }
          : prev,
      );
    },
    [],
  );

  const removeEdge = useCallback((id: string) => {
    setDraftGraph((prev) =>
      prev ? { ...prev, edges: prev.edges.filter((e) => e.id !== id) } : prev,
    );
    setSelection((prev) =>
      prev?.kind === "edge" && prev.id === id ? null : prev,
    );
  }, []);

  const addState = useCallback((stateId: string) => {
    setDraftGraph((prev) => {
      if (!prev || prev.nodes.some((n) => n.id === stateId)) return prev;
      const minX = prev.nodes.length
        ? Math.min(...prev.nodes.map((n) => n.layout.x))
        : 40;
      const maxY = prev.nodes.length
        ? Math.max(...prev.nodes.map((n) => n.layout.y))
        : 0;
      const node: WorkflowStateNode = {
        id: stateId,
        kind: deriveKind(stateId),
        layout: { x: minX, y: maxY + 120 },
      };
      return { ...prev, nodes: [...prev.nodes, node] };
    });
    setSelection({ kind: "node", id: stateId });
  }, []);

  const addTransitionFrom = useCallback((fromId: string) => {
    setDraftGraph((prev) => {
      if (!prev) return prev;
      const to =
        prev.nodes.find((n) => n.id !== fromId)?.id ?? fromId;
      const edge: WorkflowTransitionEdge = {
        id: nextEdgeId(prev.edges),
        from_state: fromId,
        to_state: to,
        preconditions: [],
        checks: [],
      };
      setSelection({ kind: "edge", id: edge.id });
      return { ...prev, edges: [...prev.edges, edge] };
    });
  }, []);

  const addTransition = useCallback(() => {
    if (!draftGraph || draftGraph.nodes.length === 0) return;
    const from =
      selection?.kind === "node" ? selection.id : draftGraph.nodes[0].id;
    addTransitionFrom(from);
  }, [draftGraph, selection, addTransitionFrom]);

  // --- actions ------------------------------------------------------------ //
  const onSave = useCallback(() => {
    if (!selectedName || !draftGraph || !editable || save.isPending) return;
    setActionError(null);
    save.mutate(
      { name: selectedName, body: { graph: draftGraph } },
      {
        onSuccess: (revision) => {
          setBaselineGraph(clone(revision.graph));
          setDraftGraph(clone(revision.graph));
          setIssues(revision.validation_issues);
          setValidationState(revision.validation_status);
          setSavedDraftExists(true);
        },
        onError: (err) => setActionError(actionMessage(err, "save the draft")),
      },
    );
  }, [selectedName, draftGraph, editable, save]);

  const onValidate = useCallback(() => {
    if (!selectedName || !editable || dirty || validate.isPending) return;
    setActionError(null);
    validate.mutate(selectedName, {
      onSuccess: (result) => {
        setIssues(result);
        setValidationState(
          result.some((i) => i.severity === "error") ? "invalid" : "valid",
        );
      },
      onError: (err) => setActionError(actionMessage(err, "validate")),
    });
  }, [selectedName, editable, dirty, validate]);

  const onPublish = useCallback(() => {
    if (
      !selectedName ||
      !editable ||
      dirty ||
      errors > 0 ||
      !savedDraftExists ||
      publish.isPending
    )
      return;
    setActionError(null);
    publish.mutate(selectedName, {
      onSuccess: (revision) => {
        setBaselineGraph(clone(revision.graph));
        setDraftGraph(clone(revision.graph));
        setIssues(revision.validation_issues);
        setValidationState("valid");
        setSavedDraftExists(false);
      },
      onError: (err) => setActionError(publishMessage(err)),
    });
  }, [selectedName, editable, dirty, errors, savedDraftExists, publish]);

  const onFork = useCallback(() => {
    if (!selectedName || fork.isPending) return;
    setActionError(null);
    fork.mutate(selectedName, {
      onError: (err) => setActionError(actionMessage(err, "fork this workflow")),
    });
  }, [selectedName, fork]);

  // --- keyboard ----------------------------------------------------------- //
  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape" && !isEditableTarget(event.target)) {
        setSelection(null);
      }
    },
    [],
  );

  // --- command palette ---------------------------------------------------- //
  const handlers = useRef({ onSave, onValidate, onPublish, addTransition });
  useEffect(() => {
    handlers.current = { onSave, onValidate, onPublish, addTransition };
  }, [onSave, onValidate, onPublish, addTransition]);
  const commands = useMemo(
    () => [
      {
        id: "new-workflow",
        label: "New workflow",
        group: "Workflows",
        icon: <Plus />,
        run: () => setNewOpen(true),
      },
      {
        id: "save-workflow-draft",
        label: "Save workflow draft",
        group: "Workflows",
        icon: <Save />,
        shortcut: "S",
        run: () => handlers.current.onSave(),
      },
      {
        id: "validate-workflow",
        label: "Validate workflow",
        group: "Workflows",
        icon: <ShieldCheck />,
        run: () => handlers.current.onValidate(),
      },
      {
        id: "publish-workflow",
        label: "Publish workflow",
        group: "Workflows",
        icon: <CheckCircle2 />,
        run: () => handlers.current.onPublish(),
      },
    ],
    [],
  );
  useRegisterCommands("workflow", commands);

  const statesNotPresent = useMemo(() => {
    const present = new Set(draftGraph?.nodes.map((n) => n.id) ?? []);
    return (catalogQuery.data?.states ?? []).filter((s) => !present.has(s));
  }, [catalogQuery.data, draftGraph]);

  // --- top-level states --------------------------------------------------- //
  if (definitionsQuery.isLoading && definitions.length === 0) {
    return <ScreenSkeleton />;
  }
  if (definitionsQuery.isError && definitions.length === 0) {
    return <ScreenError onRetry={() => definitionsQuery.refetch()} />;
  }

  return (
    <div
      data-testid="workflow-editor"
      role="application"
      aria-label="Workflow visual editor"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex h-full min-h-0 flex-col gap-4 outline-none"
    >
      <EditorHeader
        detail={detail}
        editable={editable}
        dirty={dirty}
        errors={errors}
        validationState={validationState}
        savedDraftExists={savedDraftExists}
        actionPending={actionPending}
        actionError={actionError}
        onSave={onSave}
        onValidate={onValidate}
        onPublish={onPublish}
        onFork={onFork}
        savePending={save.isPending}
        validatePending={validate.isPending}
        publishPending={publish.isPending}
        forkPending={fork.isPending}
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[15rem_minmax(0,1fr)_20rem]">
        <DefinitionRail
          definitions={definitions}
          selectedName={selectedName}
          isLoading={definitionsQuery.isLoading}
          isError={definitionsQuery.isError}
          onSelect={pickDefinition}
          onNew={() => setNewOpen(true)}
        />

        <div className="flex min-h-0 min-w-0 flex-col gap-2">
          {/* Structural toolbar */}
          <div className="flex flex-wrap items-center gap-2">
            <label className="sr-only" htmlFor="wf-add-state">
              Add a state
            </label>
            <select
              id="wf-add-state"
              data-testid="add-state"
              value=""
              disabled={!editable || !draftGraph || statesNotPresent.length === 0}
              onChange={(e) => {
                if (e.target.value) addState(e.target.value);
              }}
              className="h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
            >
              <option value="">+ Add state…</option>
              {statesNotPresent.map((state) => (
                <option key={state} value={state}>
                  {state}
                </option>
              ))}
            </select>
            <button
              type="button"
              data-testid="add-transition"
              disabled={!editable || !draftGraph || draftGraph.nodes.length === 0}
              onClick={addTransition}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Plus className="h-3.5 w-3.5" aria-hidden />
              Add transition
            </button>
            <span className="ml-auto flex items-center gap-3 text-[11px] text-muted-foreground">
              <LegendDot className="bg-spark" label="Initial" />
              <LegendDot className="bg-warning" label="Human gate" />
              <LegendDot className="bg-muted-foreground" label="Terminal" />
            </span>
          </div>

          <div className="min-h-0 flex-1">
            {draftGraph ? (
              <WorkflowCanvas
                graph={draftGraph}
                selection={selection}
                errorNodeIds={errorNodeIds}
                errorEdgeIds={errorEdgeIds}
                onSelectNode={selectNode}
                onSelectEdge={selectEdge}
                onClearSelection={clearSelection}
              />
            ) : detailQuery.isLoading ? (
              <CanvasSkeleton />
            ) : detailQuery.isError ? (
              <CanvasError onRetry={() => detailQuery.refetch()} />
            ) : (
              <CanvasSkeleton />
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-col gap-3">
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-card">
            {draftGraph ? (
              <WorkflowInspector
                graph={draftGraph}
                selection={selection}
                catalog={catalogQuery.data}
                editable={editable}
                issues={issues}
                onUpdateNode={updateNode}
                onUpdateEdge={updateEdge}
                onRemoveEdge={removeEdge}
                onAddTransitionFrom={addTransitionFrom}
              />
            ) : (
              <div className="flex h-full items-center justify-center p-6 text-center text-xs text-muted-foreground">
                Select a workflow to begin.
              </div>
            )}
          </div>

          <div className="rounded-lg border border-border bg-card p-3">
            <ValidationPanel
              issues={issues}
              validationState={validationState}
              onSelectIssue={setSelection}
            />
          </div>
        </div>
      </div>

      <NewWorkflowDialog
        open={newOpen}
        onOpenChange={setNewOpen}
        onCreated={(definition) => {
          setSelectedName(definition.name);
        }}
        client={client}
      />
    </div>
  );
}

// --- header --------------------------------------------------------------- //

interface EditorHeaderProps {
  detail: WorkflowDefinitionDetail | null;
  editable: boolean;
  dirty: boolean;
  errors: number;
  validationState: WorkflowValidationState;
  savedDraftExists: boolean;
  actionPending: boolean;
  actionError: string | null;
  onSave: () => void;
  onValidate: () => void;
  onPublish: () => void;
  onFork: () => void;
  savePending: boolean;
  validatePending: boolean;
  publishPending: boolean;
  forkPending: boolean;
}

function EditorHeader({
  detail,
  editable,
  dirty,
  errors,
  validationState,
  savedDraftExists,
  actionPending,
  actionError,
  onSave,
  onValidate,
  onPublish,
  onFork,
  savePending,
  validatePending,
  publishPending,
  forkPending,
}: EditorHeaderProps) {
  const canValidate = editable && savedDraftExists && !dirty;
  const canPublish = editable && savedDraftExists && !dirty && errors === 0;

  const hint = dirty
    ? "Unsaved changes — save the draft to validate or publish."
    : errors > 0
      ? `Fix ${errors} ${errors === 1 ? "error" : "errors"} to publish.`
      : null;

  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
          <Workflow className="h-5 w-5" aria-hidden />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-display text-xl font-semibold tracking-tight">
              {detail?.title ?? "Workflow editor"}
            </h1>
            {detail ? (
              <span className="font-mono text-xs text-muted-foreground">
                {detail.name}
              </span>
            ) : null}
            {dirty ? (
              <span
                data-testid="dirty-indicator"
                className="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] font-medium text-warning"
              >
                Unsaved changes
              </span>
            ) : null}
            {!editable && detail ? (
              <span className="inline-flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                Read-only
              </span>
            ) : null}
          </div>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-sm text-muted-foreground">
            <span>
              Map states and transitions, validate against the registry, and
              publish a new revision.
            </span>
          </p>
        </div>
      </div>

      <div className="flex flex-col items-end gap-1.5">
        <div className="flex items-center gap-2">
          {editable ? (
            <>
              <button
                type="button"
                data-testid="validate-button"
                onClick={onValidate}
                disabled={!canValidate || actionPending}
                className="inline-flex h-9 items-center gap-2 rounded-md px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <ShieldCheck className="h-4 w-4" aria-hidden />
                {validatePending ? "Validating…" : "Validate"}
              </button>
              <button
                type="button"
                data-testid="save-button"
                onClick={onSave}
                disabled={!dirty || actionPending}
                className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <Save className="h-4 w-4" aria-hidden />
                {savePending ? "Saving…" : "Save draft"}
              </button>
              <button
                type="button"
                data-testid="publish-button"
                onClick={onPublish}
                disabled={!canPublish || actionPending}
                className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden />
                {publishPending ? "Publishing…" : "Publish"}
              </button>
            </>
          ) : detail ? (
            <button
              type="button"
              data-testid="fork-button"
              onClick={onFork}
              disabled={forkPending}
              className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
            >
              <GitFork className="h-4 w-4" aria-hidden />
              {forkPending ? "Forking…" : "Fork to edit"}
            </button>
          ) : null}
        </div>
        <span
          data-testid="editor-status"
          role="status"
          aria-live="polite"
          className="min-h-[1rem] text-right text-[11px]"
        >
          {actionError ? (
            <span className="text-danger">{actionError}</span>
          ) : hint ? (
            <span className="text-muted-foreground">{hint}</span>
          ) : validationState === "valid" && editable && savedDraftExists ? (
            <span className="text-success">Valid — ready to publish.</span>
          ) : (
            ""
          )}
        </span>
      </div>
    </header>
  );
}

function LegendDot({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={cn("h-2 w-2 rounded-full", className)} aria-hidden />
      {label}
    </span>
  );
}

// --- error / message helpers ---------------------------------------------- //

function actionMessage(error: unknown, verb: string): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return `You don't have permission to ${verb}.`;
    if (error.status === 404)
      return "This workflow is no longer available.";
    if (error.status === 409)
      return `Couldn't ${verb} — it conflicts with the current state.`;
  }
  return `Couldn't ${verb}. Please try again.`;
}

function publishMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    const body =
      error.body && typeof error.body === "object"
        ? (error.body as { detail?: unknown }).detail
        : undefined;
    if (body && typeof body === "object" && "errors" in body) {
      const errs = (body as { errors?: unknown }).errors;
      const count = Array.isArray(errs) ? errs.length : 0;
      if (count > 0)
        return `Publish blocked: fix ${count} validation ${
          count === 1 ? "error" : "errors"
        } first.`;
    }
    return "Publish blocked by validation errors.";
  }
  if (error instanceof ApiError && error.status === 403)
    return "You need admin access to publish.";
  return "Couldn't publish. Please try again.";
}

// --- skeleton / error states ---------------------------------------------- //

function ScreenSkeleton() {
  return (
    <div
      data-testid="workflow-skeleton"
      aria-busy="true"
      className="flex flex-col gap-4"
    >
      <div className="h-10 w-72 animate-pulse rounded-lg bg-muted" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[15rem_minmax(0,1fr)_20rem]">
        <div className="h-72 animate-pulse rounded-lg border border-border bg-card" />
        <div className="h-72 animate-pulse rounded-lg border border-border bg-card" />
        <div className="h-72 animate-pulse rounded-lg border border-border bg-card" />
      </div>
    </div>
  );
}

function ScreenError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="workflow-error"
      role="status"
      className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border px-6 py-16 text-center"
    >
      <Workflow className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">Workflows unavailable</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        The workflow service is unreachable. The editor returns once it is back.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Retry
      </button>
    </div>
  );
}

function CanvasSkeleton() {
  return (
    <div
      data-testid="canvas-skeleton"
      aria-busy="true"
      className="h-full min-h-[24rem] w-full animate-pulse rounded-lg border border-border bg-card"
    />
  );
}

function CanvasError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="canvas-error"
      role="status"
      className="flex h-full min-h-[24rem] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border text-center"
    >
      <p className="text-sm font-medium text-foreground">
        Couldn&apos;t load this workflow
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Retry
      </button>
    </div>
  );
}
