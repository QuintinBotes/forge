"use client";

/**
 * TanStack Query hooks for the F32 integration marketplace (over the
 * `/marketplace` router).
 *
 * Kept in a dedicated module (like the F23 `spec.ts`) so the marketplace surface
 * owns its own query keys + cache policy. Reads: the catalog, one package's
 * detail, and the installed set. Mutations: `preview` (dry-run), `install`, and
 * `update` — each invalidates the installed set so a fresh install/update lights
 * up instantly, and re-reads the catalog so `available_version` is authoritative.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiClient, type ForgeApiClient } from "./client";
import type {
  Installation,
  InstallPlan,
  InstallRequest,
  InstallResult,
  Listing,
  ListingDetail,
  MarketplaceListingQuery,
} from "./types";

export const marketplaceKeys = {
  all: () => ["marketplace"] as const,
  listings: (query?: MarketplaceListingQuery) =>
    ["marketplace", "listings", query ?? {}] as const,
  listing: (registrySlug: string, slug: string) =>
    ["marketplace", "listing", registrySlug, slug] as const,
  installations: () => ["marketplace", "installations"] as const,
} as const;

/** The catalog. Passing `kind` narrows on the server; text search is client-side. */
export function useListings(
  query?: MarketplaceListingQuery,
  client: ForgeApiClient = apiClient,
): UseQueryResult<Listing[]> {
  return useQuery({
    queryKey: marketplaceKeys.listings(query),
    queryFn: () =>
      client.listListings(query as Record<string, string | number | undefined>),
  });
}

/** One package's manifest + version history. Disabled until a package is picked. */
export function useListingDetail(
  registrySlug: string | null,
  slug: string | null,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ListingDetail> {
  return useQuery({
    queryKey: marketplaceKeys.listing(registrySlug ?? "", slug ?? ""),
    queryFn: () => client.getListing(registrySlug as string, slug as string),
    enabled: Boolean(registrySlug && slug),
  });
}

/** The installed set (with any available update surfaced per row). */
export function useInstallations(
  client: ForgeApiClient = apiClient,
): UseQueryResult<Installation[]> {
  return useQuery({
    queryKey: marketplaceKeys.installations(),
    queryFn: () => client.listInstallations(),
  });
}

/**
 * Dry-run an install. A mutation (not a query) so the install dialog can fire it
 * on open and read the plan/verification imperatively — the backend surfaces a
 * hard block as a 422 which the caller renders from the {@link ApiError} body.
 */
export function usePreviewInstall(
  client: ForgeApiClient = apiClient,
): UseMutationResult<InstallPlan, Error, InstallRequest> {
  return useMutation({
    mutationFn: (request: InstallRequest) => client.previewInstall(request),
  });
}

/** Install a package, then revalidate the installed set + catalog. */
export function useInstallPackage(
  client: ForgeApiClient = apiClient,
): UseMutationResult<InstallResult, Error, InstallRequest> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: InstallRequest) => client.installPackage(request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: marketplaceKeys.all() });
    },
  });
}

export interface UpdateInstallationVariables {
  installationId: string;
  version?: string;
}

/** Update an installation, then revalidate the installed set. */
export function useUpdateInstallation(
  client: ForgeApiClient = apiClient,
): UseMutationResult<InstallResult, Error, UpdateInstallationVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ installationId, version }: UpdateInstallationVariables) =>
      client.updateInstallation(installationId, version),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: marketplaceKeys.all() });
    },
  });
}
