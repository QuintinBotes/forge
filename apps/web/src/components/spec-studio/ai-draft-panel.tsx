"use client";

import { Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useDraftSpec } from "@/lib/api/spec-studio";
import type { SpecDraft } from "@/lib/api/types";

export interface AiDraftPanelProps {
  epicId?: string;
  projectId?: string;
  client?: ForgeApiClient;
  /**
   * Called once the drafted `spec_md` has fully streamed in, handing the
   * caller the raw prose plus its parsed `SpecManifest` preview so it can
   * seed the Guided or Markdown editor.
   */
  onDraft: (draft: SpecDraft) => void;
  /** ms between reveal ticks (test hook; production default reads as "typing"). */
  revealIntervalMs?: number;
  /** Characters revealed per tick (test hook). */
  revealChunkSize?: number;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}

/**
 * `ss-ai-panel` — the AI draft-from-a-sentence entry point. Type a one-line
 * goal; `POST /spec/draft` asks the workspace's BYOK model (routed by the
 * Adaptive Orchestration model router, seeded with the project constitution)
 * to write a `spec.md`. The full draft comes back in one response (the
 * provider-side streaming already happened inside the backend call), but it
 * is *revealed* here character-by-character so the authoring experience reads
 * as the model "typing" the draft live rather than a page reflow.
 *
 * The result is always clearly marked as a draft to refine — nothing is
 * auto-saved. Once the reveal settles, `onDraft` hands the caller the parsed
 * manifest preview + raw `spec_md` so it can populate the Guided/Markdown
 * editor. The resolved model (provider + the fixed senior authoring tier) and
 * the estimated cost of the call are surfaced alongside the draft.
 */
export function AiDraftPanel({
  epicId,
  projectId,
  client = apiClient,
  onDraft,
  revealIntervalMs = 20,
  revealChunkSize = 12,
}: AiDraftPanelProps) {
  const [goal, setGoal] = useState("");
  const [revealed, setRevealed] = useState("");
  const draftSpec = useDraftSpec(client);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fullText = draftSpec.data?.spec_md ?? "";
  const streaming = draftSpec.isSuccess && revealed.length < fullText.length;

  useEffect(
    () => () => {
      if (timerRef.current) clearInterval(timerRef.current);
    },
    [],
  );

  function handleDraft() {
    const trimmed = goal.trim();
    if (!trimmed) return;
    setRevealed("");
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    draftSpec.mutate(
      { goal: trimmed, epic_id: epicId, project_id: projectId },
      {
        onSuccess: (result) => {
          const text = result.spec_md ?? "";
          if (!text) return;
          let index = 0;
          timerRef.current = setInterval(() => {
            index = Math.min(text.length, index + revealChunkSize);
            setRevealed(text.slice(0, index));
            // Hand off to the caller only once the live reveal has fully
            // caught up with the drafted text (not the instant the response
            // arrived), so the caller only ever sees the "completed" draft —
            // matching what the user just watched stream in — and exactly
            // once per draft.
            if (index >= text.length) {
              if (timerRef.current) {
                clearInterval(timerRef.current);
                timerRef.current = null;
              }
              onDraft(result);
            }
          }, revealIntervalMs);
        },
      },
    );
  }

  const usage = draftSpec.data?.usage;

  return (
    <div
      className="flex flex-col gap-3 rounded-lg border border-border bg-card/60 p-4"
      data-testid="ai-draft-panel"
    >
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" aria-hidden />
        <h3 className="font-display text-sm font-semibold text-foreground">Draft with AI</h3>
      </div>
      <p className="text-xs text-muted-foreground">
        Describe the goal in one line — a draft spec.md streams in below. It&rsquo;s a starting
        point: review and refine before saving.
      </p>
      <div className="flex gap-2">
        <input
          data-testid="ai-draft-goal"
          aria-label="One-line goal"
          value={goal}
          onChange={(event) => setGoal(event.target.value)}
          placeholder="e.g. Let customers search orders by name"
          disabled={draftSpec.isPending}
          className="flex-1 rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button
          type="button"
          onClick={handleDraft}
          disabled={!goal.trim() || draftSpec.isPending}
          data-testid="ai-draft-submit"
        >
          {draftSpec.isPending ? "Drafting…" : "Draft with AI"}
        </Button>
      </div>

      {draftSpec.isError ? (
        <p role="alert" className="text-xs text-danger" data-testid="ai-draft-error">
          {errorMessage(draftSpec.error)}
        </p>
      ) : null}

      {draftSpec.isSuccess ? (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span
              className="inline-flex items-center rounded-full border border-warning/30 bg-warning/5 px-2 py-0.5 font-medium text-warning"
              data-testid="ai-draft-badge"
            >
              Draft — review before saving
            </span>
            <span
              className="rounded-full border border-border bg-muted/40 px-2 py-0.5 font-mono"
              data-testid="ai-draft-model"
            >
              {draftSpec.data.model} · senior tier
            </span>
            {typeof usage?.cost_usd === "number" ? (
              <span
                className="rounded-full border border-border bg-muted/40 px-2 py-0.5 font-mono"
                data-testid="ai-draft-cost"
              >
                ${usage.cost_usd.toFixed(4)}
              </span>
            ) : null}
            {streaming ? <span data-testid="ai-draft-streaming">Streaming…</span> : null}
          </div>
          <pre
            data-testid="ai-draft-stream"
            className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-card px-3 py-2 font-mono text-xs leading-5 text-foreground"
          >
            {revealed}
          </pre>
          {draftSpec.data.parse_error ? (
            <p className="text-xs text-danger" data-testid="ai-draft-parse-error">
              Draft didn&rsquo;t fully parse: {draftSpec.data.parse_error}. You can still edit it
              as Markdown.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
