import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ForgeApiClient } from "./client";
import {
  ssoKeys,
  useCreateScimToken,
  useOidcConfig,
  usePutOidcConfig,
  useSetSsoEnabled,
  useSsoConfig,
} from "./sso";
import type { OidcConfig, ScimTokenCreated, SsoConfig } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function config(): SsoConfig {
  return {
    id: "c1",
    workspace_id: "w1",
    protocol: "saml",
    enabled: true,
    idp: {
      entity_id: "e",
      sso_url: "s",
      slo_url: null,
      x509_certs: ["c"],
      name_id_format: "f",
    },
    sp_entity_id: "spe",
    sp_acs_url: "https://forge.example.com/auth/saml/acme/acs",
    sp_slo_url: "spslo",
    sp_metadata_url: "spm",
    sp_cert_pem: "pem",
    domains: [],
    allow_idp_initiated: false,
    sign_authn_requests: true,
    want_assertions_signed: true,
    attribute_mapping: { email: "" },
    default_role: "member",
    group_role_map: {},
    jit_provisioning: true,
  };
}

function oidcConfig(): OidcConfig {
  return {
    id: "oc1",
    workspace_id: "w1",
    protocol: "oidc",
    enabled: true,
    issuer: "https://idp.example.com",
    discovery_url: null,
    client_id: "forge-oidc-client",
    has_client_secret: true,
    scopes: ["openid", "email", "profile"],
    email_claim: "email",
    name_claim: "name",
    groups_claim: "groups",
    default_role: "member",
    group_role_map: {},
    authorization_endpoint: null,
    token_endpoint: null,
    jwks_uri: null,
    jit_provisioning: true,
    redirect_uri: "https://forge.example.com/auth/oidc/acme/callback",
    login_url: "https://forge.example.com/auth/oidc/acme/login",
  };
}

describe("useSsoConfig", () => {
  it("resolves a 404 (not configured) to null rather than erroring", async () => {
    const client = {
      getSsoConfig: vi.fn(() =>
        Promise.reject(new ApiError(404, "no config", null)),
      ),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useSsoConfig("w1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it("propagates non-404 errors", async () => {
    const client = {
      getSsoConfig: vi.fn(() => Promise.reject(new ApiError(500, "boom", null))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useSsoConfig("w1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useSetSsoEnabled", () => {
  it("seeds the config cache with the returned config", async () => {
    const enabled = config();
    const client = {
      enableSso: vi.fn(() => Promise.resolve(enabled)),
      disableSso: vi.fn(),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();

    const { result } = renderHook(() => useSetSsoEnabled(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({ workspaceId: "w1", enabled: true });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.enableSso).toHaveBeenCalledWith("w1");
    expect(queryClient.getQueryData(ssoKeys.config("w1"))).toEqual(enabled);
  });

  it("routes disable to the disable endpoint", async () => {
    const client = {
      enableSso: vi.fn(),
      disableSso: vi.fn(() => Promise.resolve(config())),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useSetSsoEnabled(client), {
      wrapper: makeWrapper(newClient()),
    });

    result.current.mutate({ workspaceId: "w1", enabled: false });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.disableSso).toHaveBeenCalledWith("w1");
  });
});

describe("useOidcConfig", () => {
  it("resolves a 404 (not configured) to null rather than erroring", async () => {
    const client = {
      getOidcConfig: vi.fn(() =>
        Promise.reject(new ApiError(404, "no oidc config", null)),
      ),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useOidcConfig("w1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it("propagates non-404 errors", async () => {
    const client = {
      getOidcConfig: vi.fn(() => Promise.reject(new ApiError(500, "boom", null))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useOidcConfig("w1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });

  it("resolves the config on success", async () => {
    const client = {
      getOidcConfig: vi.fn(() => Promise.resolve(oidcConfig())),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useOidcConfig("w1", client), {
      wrapper: makeWrapper(newClient()),
    });

    await waitFor(() => expect(result.current.data).toEqual(oidcConfig()));
  });
});

describe("usePutOidcConfig", () => {
  it("seeds the OIDC config cache with the returned config", async () => {
    const saved = oidcConfig();
    const client = {
      putOidcConfig: vi.fn(() => Promise.resolve(saved)),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();

    const { result } = renderHook(() => usePutOidcConfig(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({
      workspaceId: "w1",
      body: { issuer: "https://idp.example.com", client_id: "forge-oidc-client" },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.putOidcConfig).toHaveBeenCalledWith("w1", {
      issuer: "https://idp.example.com",
      client_id: "forge-oidc-client",
    });
    expect(queryClient.getQueryData(ssoKeys.oidcConfig("w1"))).toEqual(saved);
  });
});

describe("useCreateScimToken", () => {
  it("invalidates the token list after issuing", async () => {
    const created: ScimTokenCreated = {
      id: "t1",
      name: "okta",
      token_prefix: "forge_sc",
      created_at: "2026-07-05T00:00:00Z",
      token: "forge_scim_secret",
    };
    const client = {
      createScimToken: vi.fn(() => Promise.resolve(created)),
    } as unknown as ForgeApiClient;
    const queryClient = newClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useCreateScimToken(client), {
      wrapper: makeWrapper(queryClient),
    });

    result.current.mutate({ workspaceId: "w1", body: { name: "okta" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.createScimToken).toHaveBeenCalledWith("w1", { name: "okta" });
    expect(spy).toHaveBeenCalledWith({ queryKey: ssoKeys.scimTokens("w1") });
  });
});
