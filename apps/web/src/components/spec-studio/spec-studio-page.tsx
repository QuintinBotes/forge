"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { LifecycleStepper } from "@/components/spec/lifecycle-stepper";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useSpecStudioManifest } from "@/lib/api/spec-studio";

import { SpecStudio } from "./spec-studio";

export interface SpecStudioPageProps {
  specId: string;
  client?: ForgeApiClient;
}

/**
 * `/specs/{id}` — a dedicated, full-page Spec Studio for one spec. Defaults
 * to Guided mode (the friendliest surface) with Markdown, YAML and Read one
 * tab away; the same round-tripping `SpecStudio` embedded in the F23
 * dashboard's "Studio" tab, given its own URL so a spec can be linked to,
 * bookmarked and deep-linked directly.
 */
export function SpecStudioPage({ specId, client = apiClient }: SpecStudioPageProps) {
  const manifestQuery = useSpecStudioManifest(specId, client);
  const name = manifestQuery.data?.name;

  return (
    <div className="flex flex-col gap-4" data-testid="spec-studio-page">
      <div className="flex flex-col gap-1">
        <Link
          href="/specs"
          className="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          Back to spec validation
        </Link>
        <h1 className="font-display text-xl font-semibold tracking-tight text-foreground">
          {name ?? "Spec"}
        </h1>
      </div>
      {manifestQuery.data ? (
        <LifecycleStepper spec={manifestQuery.data} client={client} />
      ) : null}
      <SpecStudio specId={specId} client={client} />
    </div>
  );
}
