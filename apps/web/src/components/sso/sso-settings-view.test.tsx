import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/command-palette";
import { ApiError, type ForgeApiClient } from "@/lib/api/client";
import type {
  ScimTokenCreated,
  ScimTokenInfo,
  SsoConfig,
} from "@/lib/api/types";

import { SsoSettingsView } from "./sso-settings-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const WORKSPACE = "w-test";

function makeConfig(over: Partial<SsoConfig> = {}): SsoConfig {
  return {
    id: "c1",
    workspace_id: WORKSPACE,
    protocol: "saml",
    enabled: true,
    idp: {
      entity_id: "https://idp.acme.com/saml/metadata",
      sso_url: "https://idp.acme.com/sso",
      slo_url: null,
      x509_certs: ["-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"],
      name_id_format: "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    },
    sp_entity_id: "https://forge.example.com/auth/saml/acme/metadata",
    sp_acs_url: "https://forge.example.com/auth/saml/acme/acs",
    sp_slo_url: "https://forge.example.com/auth/saml/acme/slo",
    sp_metadata_url: "https://forge.example.com/auth/saml/acme/metadata",
    sp_cert_pem: "-----BEGIN CERTIFICATE-----\nSPSP\n-----END CERTIFICATE-----",
    domains: ["acme.com"],
    allow_idp_initiated: false,
    sign_authn_requests: true,
    want_assertions_signed: true,
    attribute_mapping: { email: "" },
    default_role: "member",
    group_role_map: {},
    jit_provisioning: true,
    last_metadata_refresh_at: null,
    ...over,
  };
}

const TOKENS: ScimTokenInfo[] = [
  {
    id: "tok-1",
    name: "Okta production",
    token_prefix: "forge_sc",
    created_at: "2026-07-01T00:00:00Z",
    last_used_at: "2026-07-05T09:00:00Z",
    expires_at: null,
    revoked_at: null,
  },
];

function makeClient(overrides: Partial<ForgeApiClient> = {}): ForgeApiClient {
  return {
    baseUrl: "http://localhost:8000",
    getSsoConfig: vi.fn(() => Promise.resolve(makeConfig())),
    putSsoConfig: vi.fn((_w: string, body) =>
      Promise.resolve(makeConfig(body as Partial<SsoConfig>)),
    ),
    enableSso: vi.fn(() => Promise.resolve(makeConfig({ enabled: true }))),
    disableSso: vi.fn(() => Promise.resolve(makeConfig({ enabled: false }))),
    listScimTokens: vi.fn(() => Promise.resolve(TOKENS)),
    createScimToken: vi.fn(
      (_w: string, body: { name: string }): Promise<ScimTokenCreated> =>
        Promise.resolve({
          id: "tok-new",
          name: body.name,
          token_prefix: "forge_sc",
          created_at: "2026-07-05T12:00:00Z",
          last_used_at: null,
          expires_at: null,
          revoked_at: null,
          token: "forge_scim_SECRET123",
        }),
    ),
    revokeScimToken: vi.fn(() => Promise.resolve(undefined)),
    discoverSso: vi.fn(() => Promise.resolve({ sso: true, redirect: "/x" })),
    ...overrides,
  } as unknown as ForgeApiClient;
}

function renderView(client: ForgeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </QueryClientProvider>
    );
  }
  return render(
    <SsoSettingsView workspaceId={WORKSPACE} client={client} />,
    { wrapper: Wrapper },
  );
}

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn(() => Promise.resolve()) },
    configurable: true,
  });
});

describe("SsoSettingsView", () => {
  it("renders the loading skeleton while the config loads", () => {
    renderView(
      makeClient({ getSsoConfig: vi.fn(() => new Promise<SsoConfig>(() => {})) }),
    );
    expect(screen.getByTestId("sso-skeleton")).toBeInTheDocument();
  });

  it("shows the error state when the config service fails (non-404)", async () => {
    renderView(
      makeClient({
        getSsoConfig: vi.fn(() => Promise.reject(new ApiError(500, "boom", null))),
      }),
    );
    expect(await screen.findByTestId("sso-error")).toBeInTheDocument();
  });

  it("renders the not-configured onboarding on a 404", async () => {
    renderView(
      makeClient({
        getSsoConfig: vi.fn(() =>
          Promise.reject(new ApiError(404, "no config", null)),
        ),
      }),
    );
    expect(await screen.findByTestId("sso-onboarding")).toBeInTheDocument();
    // Trust link reads "Not linked" and SP details are pending.
    expect(screen.getByTestId("trust-state")).toHaveTextContent(/not linked/i);
    expect(screen.getByTestId("sp-pending")).toBeInTheDocument();
    // The master switch can't be enabled before there is a config.
    expect(screen.getByRole("switch", { name: /sso disabled/i })).toBeDisabled();
    // Primary action reflects the create path.
    expect(screen.getByTestId("sso-save")).toHaveTextContent(/establish trust/i);
  });

  it("renders a configured federation with IdP, SP and domains", async () => {
    renderView(makeClient());

    expect(await screen.findByTestId("trust-state")).toHaveTextContent(
      /trust established/i,
    );
    // IdP fields are seeded from the config.
    expect(screen.getByLabelText(/IdP Entity ID/i)).toHaveValue(
      "https://idp.acme.com/saml/metadata",
    );
    // SP details are exposed to hand back to the IdP.
    expect(
      screen.getByRole("button", { name: "Copy ACS URL" }),
    ).toBeInTheDocument();
    // Verified domain chip.
    expect(within(screen.getByTestId("domain-list")).getByText("acme.com")).toBeInTheDocument();
    // SCIM token row.
    expect(await screen.findByTestId("scim-token-row")).toHaveTextContent(
      "Okta production",
    );
  });

  it("flips the master switch off (calls disable)", async () => {
    const client = makeClient();
    renderView(client);
    const toggle = await screen.findByRole("switch", { name: /sso enabled/i });

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(client.disableSso).toHaveBeenCalledWith(WORKSPACE),
    );
  });

  it("enables SSO when it is configured but off", async () => {
    const client = makeClient({
      getSsoConfig: vi.fn(() => Promise.resolve(makeConfig({ enabled: false }))),
    });
    renderView(client);
    const toggle = await screen.findByRole("switch", { name: /sso disabled/i });

    fireEvent.click(toggle);

    await waitFor(() => expect(client.enableSso).toHaveBeenCalledWith(WORKSPACE));
  });

  it("saves an edited IdP configuration", async () => {
    const client = makeClient();
    renderView(client);
    const ssoUrl = await screen.findByLabelText(/IdP SSO URL/i);

    fireEvent.change(ssoUrl, {
      target: { value: "https://idp.acme.com/sso/v2" },
    });
    fireEvent.click(screen.getByTestId("sso-save"));

    await waitFor(() =>
      expect(client.putSsoConfig).toHaveBeenCalledWith(
        WORKSPACE,
        expect.objectContaining({
          idp: expect.objectContaining({ sso_url: "https://idp.acme.com/sso/v2" }),
        }),
      ),
    );
  });

  it("adds a login domain to the verified list", async () => {
    renderView(makeClient());
    const input = await screen.findByLabelText("Add domain");

    fireEvent.change(input, { target: { value: "beta.acme.io" } });
    fireEvent.click(screen.getByRole("button", { name: "Add domain" }));

    expect(
      within(screen.getByTestId("domain-list")).getByText("beta.acme.io"),
    ).toBeInTheDocument();
  });

  it("probes home-realm discovery and reports an SSO domain", async () => {
    const client = makeClient();
    renderView(client);
    const email = await screen.findByLabelText("Test login email");

    fireEvent.change(email, { target: { value: "sam@acme.com" } });
    fireEvent.click(screen.getByTestId("hrd-test"));

    await waitFor(() =>
      expect(client.discoverSso).toHaveBeenCalledWith({ email: "sam@acme.com" }),
    );
    expect(await screen.findByTestId("hrd-result")).toHaveTextContent(
      /routes to sso/i,
    );
  });

  it("reports a non-SSO domain from home-realm discovery", async () => {
    const client = makeClient({
      discoverSso: vi.fn(() => Promise.resolve({ sso: false })),
    });
    renderView(client);
    fireEvent.change(await screen.findByLabelText("Test login email"), {
      target: { value: "sam@gmail.com" },
    });
    fireEvent.click(screen.getByTestId("hrd-test"));

    expect(await screen.findByTestId("hrd-result")).toHaveTextContent(
      /no sso for this domain/i,
    );
  });

  it("issues a SCIM token and reveals the secret once", async () => {
    const client = makeClient();
    renderView(client);

    fireEvent.click(await screen.findByTestId("scim-create-open"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/token name/i), {
      target: { value: "Azure AD" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /issue token/i }));

    await waitFor(() =>
      expect(client.createScimToken).toHaveBeenCalledWith(WORKSPACE, {
        name: "Azure AD",
        expires_at: null,
      }),
    );
    // The plaintext token is shown exactly once.
    expect(await screen.findByTestId("scim-created")).toHaveTextContent(
      "forge_scim_SECRET123",
    );
  });

  it("revokes an active SCIM token", async () => {
    const client = makeClient();
    renderView(client);

    fireEvent.click(await screen.findByTestId("scim-revoke"));

    await waitFor(() =>
      expect(client.revokeScimToken).toHaveBeenCalledWith(WORKSPACE, "tok-1"),
    );
  });

  it("shows the SCIM empty state with no tokens", async () => {
    renderView(makeClient({ listScimTokens: vi.fn(() => Promise.resolve([])) }));
    expect(await screen.findByTestId("scim-empty")).toBeInTheDocument();
  });

  it("copies a service-provider value to the clipboard", async () => {
    renderView(makeClient());
    const copy = await screen.findByRole("button", { name: "Copy ACS URL" });

    fireEvent.click(copy);

    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "https://forge.example.com/auth/saml/acme/acs",
      ),
    );
  });
});
