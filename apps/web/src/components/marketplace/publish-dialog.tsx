"use client";

import { AlertTriangle, Upload } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import { usePublishListing, useRegistries } from "@/lib/api/marketplace";
import type { ArtifactKind, ListingPublishRequest } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { kindLabel } from "./marketplace-meta";

export interface PublishDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  client?: ForgeApiClient;
}

const PUBLISHABLE_KINDS: ArtifactKind[] = ["skill_profile", "mcp_connector"];

const DEFAULT_ARTIFACT: Record<ArtifactKind, string> = {
  skill_profile: `{\n  "name": "",\n  "description": ""\n}`,
  mcp_connector: `{\n  "name": "",\n  "transport": "http",\n  "endpoint": "https://",\n  "allowed_namespaces": []\n}`,
  workflow_template: "{}",
  policy_template: "{}",
};

const inputClass = cn(
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none",
  "focus-visible:ring-2 focus-visible:ring-ring",
);

/** Read a plain-string 422/409 detail out of a publish failure. */
function errorMessageFrom(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown } | null;
    if (typeof body?.detail === "string") return body.detail;
  }
  if (error instanceof Error) return error.message;
  return "Publish failed.";
}

/**
 * The in-app publish flow (F32 marketplace-publish): author a package straight
 * into a registry the workspace owns, without leaving the app. Mirrors the
 * offline `forge marketplace package` CLI step — the artifact is validated
 * server-side through the exact same installer schema, so a schema-invalid
 * submission is rejected (422) rather than silently cached.
 */
export function PublishDialog({ open, onOpenChange, client }: PublishDialogProps) {
  const registriesQuery = useRegistries(client);
  const publish = usePublishListing(client);

  const registries = (registriesQuery.data ?? []).filter((r) => r.slug !== "official");

  const [registryId, setRegistryId] = useState("");
  const [kind, setKind] = useState<ArtifactKind>("skill_profile");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState("");
  const [artifactText, setArtifactText] = useState(DEFAULT_ARTIFACT.skill_profile);
  const [artifactError, setArtifactError] = useState<string | null>(null);

  const effectiveRegistryId = registryId || registries[0]?.id || "";

  const reset = () => {
    setRegistryId("");
    setKind("skill_profile");
    setSlug("");
    setName("");
    setVersion("1.0.0");
    setSummary("");
    setTags("");
    setArtifactText(DEFAULT_ARTIFACT.skill_profile);
    setArtifactError(null);
    publish.reset();
  };

  const onKindChange = (next: ArtifactKind) => {
    setKind(next);
    setArtifactText(DEFAULT_ARTIFACT[next]);
    setArtifactError(null);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setArtifactError(null);

    let artifact: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(artifactText);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("must be a JSON object");
      }
      artifact = parsed as Record<string, unknown>;
    } catch {
      setArtifactError("Artifact must be valid JSON (an object).");
      return;
    }

    if (!effectiveRegistryId || !slug.trim() || !name.trim() || !summary.trim()) return;

    const request: ListingPublishRequest = {
      registry_id: effectiveRegistryId,
      kind,
      slug: slug.trim(),
      name: name.trim(),
      version: version.trim(),
      summary: summary.trim(),
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      artifact,
    };

    publish.mutate(request, {
      onSuccess: (listing) => {
        toast.success(`Published ${listing.name} · v${listing.latest_version}`);
        reset();
        onOpenChange(false);
      },
    });
  };

  const canSubmit =
    Boolean(effectiveRegistryId) &&
    slug.trim().length > 0 &&
    name.trim().length > 0 &&
    version.trim().length > 0 &&
    summary.trim().length > 0 &&
    !publish.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent
        data-testid="publish-dialog"
        className="max-w-lg gap-5"
        aria-describedby="publish-dialog-desc"
      >
        <DialogHeader>
          <DialogTitle className="font-display">Publish a package</DialogTitle>
          <DialogDescription id="publish-dialog-desc">
            Author a skill profile or MCP connector straight into a registry your
            workspace owns — the offline CLI publishing step, in-app.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-4">
          {registries.length === 0 ? (
            <div
              role="alert"
              data-testid="publish-no-registries"
              className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2.5 text-xs text-foreground"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
              No registry available to publish into yet. Add a registry first (the
              read-only official registry cannot accept publishes).
            </div>
          ) : null}

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Registry</span>
              <select
                value={effectiveRegistryId}
                onChange={(e) => setRegistryId(e.target.value)}
                disabled={registries.length === 0}
                aria-label="Registry"
                className={inputClass}
              >
                {registries.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.slug}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Kind</span>
              <select
                value={kind}
                onChange={(e) => onKindChange(e.target.value as ArtifactKind)}
                aria-label="Kind"
                className={inputClass}
              >
                {PUBLISHABLE_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {kindLabel(k)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Slug</span>
              <input
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="my-connector"
                aria-label="Slug"
                className={inputClass}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Version</span>
              <input
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                placeholder="1.0.0"
                aria-label="Version"
                className={cn(inputClass, "font-mono")}
              />
            </label>
          </div>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My Connector"
              aria-label="Name"
              className={inputClass}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Summary</span>
            <input
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="One line describing what it does"
              aria-label="Summary"
              className={inputClass}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Tags (comma-separated)</span>
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="python, backend"
              aria-label="Tags"
              className={inputClass}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Artifact (JSON)</span>
            <textarea
              value={artifactText}
              onChange={(e) => setArtifactText(e.target.value)}
              rows={6}
              aria-label="Artifact JSON"
              spellCheck={false}
              className={cn(
                "w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none",
                "focus-visible:ring-2 focus-visible:ring-ring",
              )}
            />
            {artifactError ? (
              <span role="alert" className="text-xs text-danger">
                {artifactError}
              </span>
            ) : null}
          </label>

          {publish.isError ? (
            <p role="alert" data-testid="publish-error" className="text-xs text-danger">
              {errorMessageFrom(publish.error)}
            </p>
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => onOpenChange(false)}
              disabled={publish.isPending}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              data-testid="confirm-publish"
              disabled={!canSubmit}
            >
              <Upload className="h-4 w-4" aria-hidden />
              {publish.isPending ? "Publishing…" : "Publish"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
