"use client";

/**
 * Knowledge search and the document view. The envelope's retrieval legs are
 * shown exactly as returned, `unconfigured` and `fallback` included
 * (ADR-0018). Passage and document text is rendered only as text, never as
 * HTML or markdown, and tracker items, commit messages and MCP resources are
 * labelled as external content (ADR-0014): they are untrusted text.
 */

import { useQuery } from "@tanstack/react-query";
import { SearchX } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent } from "react";

import { errorDetail } from "@/components/world/world-format";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import type { ForgeApiClient } from "@/lib/api/client";
import type {
  KnowledgePassage,
  KnowledgeSearchResponse,
  KnowledgeSourceSummary,
} from "@/lib/api/types";

/** Source kinds `scope.source_types` filters on. */
const SOURCE_KINDS = ["repo", "url", "document", "mcp"] as const;

const TRACKER_ITEM = /^(issues|pulls)\/\d+$/;
const COMMIT = /^commits\/[0-9a-f]+$/;

/** What kind of external (untrusted) content a passage is, else null. */
export function externalContent(passage: KnowledgePassage): string | null {
  const kind = passage.metadata?.source_kind;
  const path = passage.path ?? "";
  if (kind === "mcp") {
    return "MCP resource";
  }
  if (kind === "url" && TRACKER_ITEM.test(path)) {
    return "tracker item";
  }
  if (kind === "repo" && COMMIT.test(path)) {
    return "commit message";
  }
  return null;
}

function ExternalBadge({ passage }: { passage: KnowledgePassage }) {
  const external = externalContent(passage);
  if (!external) {
    return null;
  }
  return (
    <span className="rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
      External content ({external})
    </span>
  );
}

function PlainText({ text }: { text: string }) {
  return (
    <pre className="whitespace-pre-wrap break-words rounded-md bg-muted/50 p-3 font-mono text-xs text-foreground">
      {text}
    </pre>
  );
}

function LegsLine({ data }: { data: KnowledgeSearchResponse }) {
  return (
    <p data-testid="knowledge-legs" className="text-sm text-muted-foreground">
      Legs that ran:{" "}
      <span className="font-medium text-foreground">
        {data.legs.length > 0 ? data.legs.join(" + ") : "none"}
      </span>{" "}
      · vector leg:{" "}
      <span className="font-medium text-foreground">{data.vector_leg}</span>
      {data.vector_leg === "unconfigured" ? " (no embedder: keyword search only)" : ""}{" "}
      · reranker: <span className="font-medium text-foreground">{data.rerank}</span>
      {data.rerank === "fallback" ? " (the reranker failed on this call: fused order shown)" : ""}
    </p>
  );
}

export interface KnowledgeSearchProps {
  client: ForgeApiClient;
  sources?: KnowledgeSourceSummary[];
}

export function KnowledgeSearch({ client, sources }: KnowledgeSearchProps) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("");
  const [submitted, setSubmitted] = useState<{ query: string; kind: string } | null>(
    null,
  );
  const [open, setOpen] = useState<KnowledgePassage | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  const names = useMemo(
    () => new Map((sources ?? []).map((source) => [source.id, source.name])),
    [sources],
  );
  const sourceName = (passage: KnowledgePassage) =>
    names.get(passage.source_id ?? "") ?? passage.source_uri ?? passage.source;

  const search = useQuery({
    queryKey: ["knowledge", "search", submitted],
    queryFn: () =>
      client.searchKnowledge({
        query: submitted?.query ?? "",
        scope: submitted?.kind ? { source_types: [submitted.kind] } : undefined,
      }),
    enabled: submitted !== null,
  });

  const doc = useQuery({
    queryKey: ["knowledge", "document", open?.source_id, open?.path],
    queryFn: () =>
      client.getKnowledgeDocument(open?.source_id ?? "", open?.path ?? ""),
    enabled: Boolean(open?.source_id && open.path),
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const query = text.trim();
    if (query !== "") {
      setSubmitted({ query, kind });
    }
  };

  let results;
  if (submitted === null) {
    results = (
      <p className="text-sm text-muted-foreground">
        Search the indexed sources: files, specs, tracker items and commits.
      </p>
    );
  } else if (search.isLoading) {
    results = (
      <Loading label="Searching…">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </Loading>
    );
  } else if (search.isError) {
    results = (
      <ErrorState
        title="Search failed"
        description={errorDetail(search.error)}
        onRetry={() => void search.refetch()}
      />
    );
  } else if (search.data) {
    results = (
      <div className="flex flex-col gap-3">
        <LegsLine data={search.data} />
        {search.data.results.length === 0 ? (
          <EmptyState
            icon={<SearchX aria-hidden />}
            title="No passages match"
            description="Try other words, or another source kind."
          />
        ) : (
          <ol className="flex flex-col gap-3" aria-label="Search results">
            {search.data.results.map((passage, index) => (
              <li
                key={passage.id ?? `${passage.source}:${index}`}
                className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-card p-3"
              >
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  {passage.source_id && passage.path ? (
                    <button
                      type="button"
                      onClick={(event) => {
                        opener.current = event.currentTarget;
                        setOpen(passage);
                      }}
                      className="break-all text-left text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {passage.path}
                    </button>
                  ) : (
                    <span className="break-all text-sm font-medium text-foreground">
                      {passage.path ?? "(no path)"}
                    </span>
                  )}
                  <span>{sourceName(passage)}</span>
                  <span>{passage.chunk_type ?? "chunk"}</span>
                  <span>score {passage.score.toFixed(3)}</span>
                  {passage.rerank_score != null ? (
                    <span>rerank {passage.rerank_score.toFixed(3)}</span>
                  ) : null}
                  <ExternalBadge passage={passage} />
                </div>
                <PlainText text={passage.content} />
              </li>
            ))}
          </ol>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="knowledge-search" className="flex flex-col gap-3">
      <h2 id="knowledge-search" className="text-sm font-semibold text-foreground">
        Search
      </h2>
      <form
        role="search"
        onSubmit={onSubmit}
        className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card px-4 py-3"
      >
        <div className="flex min-w-0 flex-1 basis-56 flex-col gap-1">
          <label htmlFor="knowledge-query" className="text-sm font-medium text-foreground">
            Search knowledge
          </label>
          <input
            id="knowledge-query"
            type="search"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="e.g. token rotation policy"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="knowledge-kind" className="text-sm font-medium text-foreground">
            Source kind
          </label>
          <select
            id="knowledge-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="">All kinds</option>
            {SOURCE_KINDS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit">Search</Button>
      </form>
      {results}

      <Dialog open={open !== null} onOpenChange={(next) => !next && setOpen(null)}>
        <DialogContent
          className="max-h-[85vh] max-w-3xl overflow-y-auto"
          onCloseAutoFocus={(event) => {
            // No DialogTrigger: return focus to the hit that opened the view.
            event.preventDefault();
            opener.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle className="break-all pr-6">{open?.path}</DialogTitle>
            <DialogDescription className="break-all">
              {open ? `${sourceName(open)} · ${open.source}` : null}
            </DialogDescription>
          </DialogHeader>
          {open ? <ExternalBadge passage={open} /> : null}
          {doc.isLoading ? (
            <Loading label="Loading document…">
              <Skeleton className="h-40 w-full" />
            </Loading>
          ) : doc.isError ? (
            <ErrorState
              title="Document didn't load"
              description={errorDetail(doc.error)}
              onRetry={() => void doc.refetch()}
            />
          ) : doc.data ? (
            <div className="flex min-w-0 flex-col gap-2">
              {doc.data.truncated ? (
                <p className="text-sm text-warning">
                  Truncated: showing {doc.data.passages.length} of{" "}
                  {doc.data.total_chunks} chunks ({doc.data.tokens} tokens).
                </p>
              ) : null}
              {doc.data.passages.map((passage, index) => (
                <PlainText key={passage.id ?? index} text={passage.content} />
              ))}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </section>
  );
}
