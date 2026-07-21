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
  OidcConfig,
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

function makeOidcConfig(over: Partial<OidcConfig> = {}): OidcConfig {
  return {
    id: "oc1",
    workspace_id: WORKSPACE,
    protocol: "oidc",
    enabled: true,
    issuer: "https://idp.acme.com",
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
    getOidcConfig: vi.fn(() =>
      Promise.reject(new ApiError(404, "no oidc config", null)),
    ),
    putOidcConfig: vi.fn((_w: string, body) =>
      Promise.resolve(makeOidcConfig(body as Partial<OidcConfig>)),
    ),
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

  it("renders the SLO URL field disabled with a not-yet-supported hint, but still shows a previously saved value", async () => {
    const client = makeClient({
      getSsoConfig: vi.fn(() =>
        Promise.resolve(
          makeConfig({
            idp: {
              entity_id: "https://idp.acme.com/saml/metadata",
              sso_url: "https://idp.acme.com/sso",
              slo_url: "https://idp.acme.com/slo",
              x509_certs: [
                "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
              ],
              name_id_format:
                "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            },
          }),
        ),
      ),
    });
    renderView(client);

    const sloField = await screen.findByLabelText(/IdP SLO URL/i);
    expect(sloField).toBeDisabled();
    // Disabled != hidden: an admin who previously saved a value should still see it.
    expect(sloField).toHaveValue("https://idp.acme.com/slo");
    expect(
      screen.getByText(/single logout is not yet supported/i),
    ).toBeInTheDocument();
    // House style bans "coming soon" phrasing everywhere in this view.
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
  });

  it("keeps the saved SLO URL in the save payload even though the field is disabled", async () => {
    const client = makeClient({
      getSsoConfig: vi.fn(() =>
        Promise.resolve(
          makeConfig({
            idp: {
              entity_id: "https://idp.acme.com/saml/metadata",
              sso_url: "https://idp.acme.com/sso",
              slo_url: "https://idp.acme.com/slo",
              x509_certs: [
                "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
              ],
              name_id_format:
                "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            },
          }),
        ),
      ),
    });
    renderView(client);
    await screen.findByLabelText(/IdP SLO URL/i);

    fireEvent.click(screen.getByTestId("sso-save"));

    await waitFor(() =>
      expect(client.putSsoConfig).toHaveBeenCalledWith(
        WORKSPACE,
        expect.objectContaining({
          idp: expect.objectContaining({
            slo_url: "https://idp.acme.com/slo",
          }),
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

describe("OIDC panel", () => {
  it("has no coming-soon marker — the OIDC tab is a real, clickable toggle", async () => {
    renderView(makeClient());
    await screen.findByTestId("sso-view");

    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^soon$/i)).not.toBeInTheDocument();
    const oidcTab = screen.getByTestId("protocol-oidc");
    expect(oidcTab.tagName).toBe("BUTTON");
    expect(oidcTab).not.toBeDisabled();
  });

  it("switches to the OIDC tab and shows its onboarding hint when unconfigured", async () => {
    renderView(makeClient());
    await screen.findByTestId("sso-view");

    fireEvent.click(screen.getByTestId("protocol-oidc"));

    expect(await screen.findByTestId("oidc-onboarding")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Issuer/i)).toHaveValue("");
    expect(screen.getByTestId("oidc-sp-pending")).toBeInTheDocument();
    // Creating requires a secret; the save button starts disabled.
    expect(screen.getByTestId("oidc-save")).toBeDisabled();
  });

  it("renders a configured OIDC provider and its service-provider details", async () => {
    const client = makeClient({
      getOidcConfig: vi.fn(() => Promise.resolve(makeOidcConfig())),
    });
    renderView(client);
    fireEvent.click(await screen.findByTestId("protocol-oidc"));

    expect(await screen.findByLabelText(/^Issuer/i)).toHaveValue(
      "https://idp.acme.com",
    );
    expect(screen.getByLabelText(/client id/i)).toHaveValue(
      "forge-oidc-client",
    );
    expect(
      screen.getByRole("button", { name: "Copy Redirect URI" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy Login URL" }),
    ).toBeInTheDocument();
    // Editing an existing config doesn't require re-entering the secret.
    expect(screen.getByTestId("oidc-save")).not.toBeDisabled();
  });

  it("saves a new OIDC configuration with the entered fields", async () => {
    const client = makeClient();
    renderView(client);
    fireEvent.click(await screen.findByTestId("protocol-oidc"));

    fireEvent.change(screen.getByLabelText(/^Issuer/i), {
      target: { value: "https://idp.acme.com" },
    });
    fireEvent.change(screen.getByLabelText(/client id/i), {
      target: { value: "forge-oidc-client" },
    });
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: "s3cr3t" },
    });
    expect(screen.getByTestId("oidc-save")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("oidc-save"));

    await waitFor(() =>
      expect(client.putOidcConfig).toHaveBeenCalledWith(
        WORKSPACE,
        expect.objectContaining({
          issuer: "https://idp.acme.com",
          client_id: "forge-oidc-client",
          client_secret: "s3cr3t",
          email_claim: "email",
          name_claim: "name",
          groups_claim: "groups",
          default_role: "member",
          jit_provisioning: true,
        }),
      ),
    );
  });

  it("saves an update to an existing OIDC config without resending the secret", async () => {
    const client = makeClient({
      getOidcConfig: vi.fn(() => Promise.resolve(makeOidcConfig())),
    });
    renderView(client);
    fireEvent.click(await screen.findByTestId("protocol-oidc"));

    const issuer = await screen.findByLabelText(/^Issuer/i);
    fireEvent.change(issuer, {
      target: { value: "https://idp2.acme.com" },
    });
    fireEvent.click(screen.getByTestId("oidc-save"));

    await waitFor(() =>
      expect(client.putOidcConfig).toHaveBeenCalledWith(
        WORKSPACE,
        expect.objectContaining({
          issuer: "https://idp2.acme.com",
          client_secret: null,
        }),
      ),
    );
  });

  it("adds a group role mapping and includes it on save", async () => {
    const client = makeClient();
    renderView(client);
    fireEvent.click(await screen.findByTestId("protocol-oidc"));

    fireEvent.change(await screen.findByLabelText(/^Issuer/i), {
      target: { value: "https://idp.acme.com" },
    });
    fireEvent.change(screen.getByLabelText(/client id/i), {
      target: { value: "forge-oidc-client" },
    });
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: "s3cr3t" },
    });

    fireEvent.click(screen.getByTestId("oidc-add-mapping"));
    const row = within(screen.getByTestId("role-map-list")).getByRole(
      "textbox",
    );
    fireEvent.change(row, { target: { value: "forge-admins" } });
    fireEvent.change(screen.getByRole("combobox", { name: /role for mapping/i }), {
      target: { value: "admin" },
    });

    fireEvent.click(screen.getByTestId("oidc-save"));

    await waitFor(() =>
      expect(client.putOidcConfig).toHaveBeenCalledWith(
        WORKSPACE,
        expect.objectContaining({
          group_role_map: { "forge-admins": "admin" },
        }),
      ),
    );
  });

  it("removes a group role mapping", async () => {
    renderView(makeClient());
    fireEvent.click(await screen.findByTestId("protocol-oidc"));

    fireEvent.click(screen.getByTestId("oidc-add-mapping"));
    expect(screen.getByTestId("role-map-list")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /remove mapping 1/i }));

    expect(screen.getByTestId("role-map-empty")).toBeInTheDocument();
  });
});
