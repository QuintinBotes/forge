"use client";

/**
 * TanStack Query hooks for the enterprise SSO + SCIM admin surface (F33), over
 * the `/workspaces/{id}/sso`, `/workspaces/{id}/oidc`,
 * `/workspaces/{id}/scim/tokens` and `/auth/saml/discover` routers.
 *
 * Kept in a dedicated module (like `deployments.ts` / `marketplace.ts`) so the
 * SSO surface owns its query keys + cache policy. Reads: the SAML config and
 * the OIDC config (each a distinct workspace-scoped configuration — a 404 is
 * the "not configured yet" empty state, resolved to `null`, not an error) and
 * the SCIM token list. Mutations drive each config's lifecycle — save the
 * SAML or OIDC config, flip the master SAML enable/disable switch, issue and
 * revoke SCIM tokens — and each seeds or invalidates its workspace cache so
 * the trust-link header, the forms and the token table stay coherent the
 * instant a change lands. Home-realm discovery is a mutation (a probe read
 * fired imperatively from the HRD field).
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, apiClient, type ForgeApiClient } from "./client";
import type {
  HrdDiscoverResponse,
  OidcConfig,
  OidcConfigInput,
  ScimTokenCreated,
  ScimTokenCreateRequest,
  ScimTokenInfo,
  SsoConfig,
  SsoConfigInput,
} from "./types";

export const ssoKeys = {
  all: () => ["sso"] as const,
  config: (workspaceId: string) => ["sso", "config", workspaceId] as const,
  oidcConfig: (workspaceId: string) =>
    ["sso", "oidc-config", workspaceId] as const,
  scimTokens: (workspaceId: string) =>
    ["sso", "scim-tokens", workspaceId] as const,
} as const;

/**
 * The workspace SAML configuration. A 404 (no config yet) is a normal empty
 * state, not an error, so it resolves to `null` — the screen renders its
 * "set up SAML" onboarding form rather than an error card.
 */
export function useSsoConfig(
  workspaceId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<SsoConfig | null> {
  return useQuery({
    queryKey: ssoKeys.config(workspaceId),
    queryFn: async () => {
      try {
        return await client.getSsoConfig(workspaceId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
    },
    enabled: Boolean(workspaceId),
  });
}

/**
 * The workspace OIDC configuration. Like {@link useSsoConfig}, a 404 (no
 * config yet) is a normal empty state, not an error, so it resolves to
 * `null` — the OIDC panel renders its own "set up OIDC" onboarding hint.
 */
export function useOidcConfig(
  workspaceId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<OidcConfig | null> {
  return useQuery({
    queryKey: ssoKeys.oidcConfig(workspaceId),
    queryFn: async () => {
      try {
        return await client.getOidcConfig(workspaceId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
    },
    enabled: Boolean(workspaceId),
  });
}

/** The workspace's SCIM provisioning tokens (redacted). */
export function useScimTokens(
  workspaceId: string,
  client: ForgeApiClient = apiClient,
): UseQueryResult<ScimTokenInfo[]> {
  return useQuery({
    queryKey: ssoKeys.scimTokens(workspaceId),
    queryFn: () => client.listScimTokens(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export interface PutSsoConfigVariables {
  workspaceId: string;
  body: SsoConfigInput;
}

/** Save (create or replace) the SAML config; seeds + revalidates its cache. */
export function usePutSsoConfig(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SsoConfig, Error, PutSsoConfigVariables> {
  const queryClient = useQueryClient();
  return useMutation<SsoConfig, Error, PutSsoConfigVariables>({
    mutationFn: ({ workspaceId, body }) => client.putSsoConfig(workspaceId, body),
    onSuccess: (config, { workspaceId }) => {
      queryClient.setQueryData(ssoKeys.config(workspaceId), config);
    },
    onSettled: (_data, _err, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: ssoKeys.config(workspaceId),
      });
    },
  });
}

export interface PutOidcConfigVariables {
  workspaceId: string;
  body: OidcConfigInput;
}

/** Save (create or replace) the OIDC config; seeds + revalidates its cache. */
export function usePutOidcConfig(
  client: ForgeApiClient = apiClient,
): UseMutationResult<OidcConfig, Error, PutOidcConfigVariables> {
  const queryClient = useQueryClient();
  return useMutation<OidcConfig, Error, PutOidcConfigVariables>({
    mutationFn: ({ workspaceId, body }) =>
      client.putOidcConfig(workspaceId, body),
    onSuccess: (config, { workspaceId }) => {
      queryClient.setQueryData(ssoKeys.oidcConfig(workspaceId), config);
    },
    onSettled: (_data, _err, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: ssoKeys.oidcConfig(workspaceId),
      });
    },
  });
}

export interface SetSsoEnabledVariables {
  workspaceId: string;
  enabled: boolean;
}

/** Flip the master SSO switch (enable / break-glass-guarded disable). */
export function useSetSsoEnabled(
  client: ForgeApiClient = apiClient,
): UseMutationResult<SsoConfig, Error, SetSsoEnabledVariables> {
  const queryClient = useQueryClient();
  return useMutation<SsoConfig, Error, SetSsoEnabledVariables>({
    mutationFn: ({ workspaceId, enabled }) =>
      enabled ? client.enableSso(workspaceId) : client.disableSso(workspaceId),
    onSuccess: (config, { workspaceId }) => {
      queryClient.setQueryData(ssoKeys.config(workspaceId), config);
    },
  });
}

export interface CreateScimTokenVariables {
  workspaceId: string;
  body: ScimTokenCreateRequest;
}

/** Issue a SCIM token, then revalidate the token list. */
export function useCreateScimToken(
  client: ForgeApiClient = apiClient,
): UseMutationResult<ScimTokenCreated, Error, CreateScimTokenVariables> {
  const queryClient = useQueryClient();
  return useMutation<ScimTokenCreated, Error, CreateScimTokenVariables>({
    mutationFn: ({ workspaceId, body }) =>
      client.createScimToken(workspaceId, body),
    onSuccess: (_data, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: ssoKeys.scimTokens(workspaceId),
      });
    },
  });
}

export interface RevokeScimTokenVariables {
  workspaceId: string;
  tokenId: string;
}

/** Revoke a SCIM token, then revalidate the token list. */
export function useRevokeScimToken(
  client: ForgeApiClient = apiClient,
): UseMutationResult<void, Error, RevokeScimTokenVariables> {
  const queryClient = useQueryClient();
  return useMutation<void, Error, RevokeScimTokenVariables>({
    mutationFn: ({ workspaceId, tokenId }) =>
      client.revokeScimToken(workspaceId, tokenId),
    onSettled: (_data, _err, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: ssoKeys.scimTokens(workspaceId),
      });
    },
  });
}

/**
 * Home-realm discovery probe. A mutation (not a query) so the HRD "test login
 * email" field can fire it on demand and read the routing verdict imperatively.
 */
export function useDiscoverSso(
  client: ForgeApiClient = apiClient,
): UseMutationResult<HrdDiscoverResponse, Error, string> {
  return useMutation<HrdDiscoverResponse, Error, string>({
    mutationFn: (email: string) => client.discoverSso({ email }),
  });
}
