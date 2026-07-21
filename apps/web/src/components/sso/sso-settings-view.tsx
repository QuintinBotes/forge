"use client";

import {
  ArrowRight,
  Building2,
  Fingerprint,
  Globe,
  KeyRound,
  Link2,
  Link2Off,
  PlugZap,
  Save,
  ServerCog,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { toast } from "@/components/ui/toast";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useDiscoverSso,
  useOidcConfig,
  usePutOidcConfig,
  usePutSsoConfig,
  useSetSsoEnabled,
  useSsoConfig,
} from "@/lib/api/sso";
import {
  SSO_ROLES,
  type OidcConfig,
  type SsoConfig,
  type SsoRole,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { CopyField } from "./copy-field";
import { ScimPanel } from "./scim-panel";
import {
  NAMEID_FORMAT_EMAIL,
  NAMEID_FORMATS,
  countCerts,
  federationState,
  hostLabel,
  isValidDomain,
  nameIdFormatLabel,
  normalizeDomain,
} from "./sso-meta";
import { SsoSwitch } from "./sso-switch";

/** Placeholder workspace until workspace routing lands (mirrors the board). */
export const DEFAULT_WORKSPACE_ID = "default";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const ROLE_LABELS: Record<SsoRole, string> = {
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
  "agent-runner": "Agent runner",
};

interface SamlForm {
  idpEntityId: string;
  idpSsoUrl: string;
  idpSloUrl: string;
  idpCert: string;
  nameIdFormat: string;
  defaultRole: SsoRole;
  signAuthnRequests: boolean;
  wantAssertionsSigned: boolean;
  allowIdpInitiated: boolean;
  jitProvisioning: boolean;
  domains: string[];
}

function configToForm(config: SsoConfig | null): SamlForm {
  if (!config) {
    return {
      idpEntityId: "",
      idpSsoUrl: "",
      idpSloUrl: "",
      idpCert: "",
      nameIdFormat: NAMEID_FORMAT_EMAIL,
      defaultRole: "member",
      signAuthnRequests: true,
      wantAssertionsSigned: true,
      allowIdpInitiated: false,
      jitProvisioning: true,
      domains: [],
    };
  }
  const role = (SSO_ROLES as readonly string[]).includes(config.default_role)
    ? (config.default_role as SsoRole)
    : "member";
  return {
    idpEntityId: config.idp.entity_id,
    idpSsoUrl: config.idp.sso_url,
    idpSloUrl: config.idp.slo_url ?? "",
    idpCert: config.idp.x509_certs[0] ?? "",
    nameIdFormat: config.idp.name_id_format,
    defaultRole: role,
    signAuthnRequests: config.sign_authn_requests,
    wantAssertionsSigned: config.want_assertions_signed,
    allowIdpInitiated: config.allow_idp_initiated,
    jitProvisioning: config.jit_provisioning,
    domains: [...config.domains],
  };
}

function saveErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to change SSO settings.";
    if (error.status === 409) {
      const detail =
        error.body && typeof error.body === "object"
          ? (error.body as { detail?: unknown }).detail
          : undefined;
      if (detail && typeof detail === "object" && "error" in detail) {
        const kind = (detail as { error?: string; domain?: string }).error;
        if (kind === "domain_conflict") {
          const domain = (detail as { domain?: string }).domain;
          return `The domain ${domain ?? ""} is already claimed by another workspace.`.trim();
        }
        if (kind === "last_admin")
          return "Keep at least one local admin before turning SSO off.";
      }
      return "That change conflicts with the current configuration.";
    }
    if (error.status === 400)
      return "Some SAML details look invalid. Check the IdP fields and try again.";
  }
  return "Couldn't save the configuration. Please try again.";
}

/** The SSO protocol tab currently shown in the "Identity provider" card. */
type SsoProtocol = "saml" | "oidc";

const DEFAULT_OIDC_SCOPES = "openid, email, profile";

interface GroupRoleMapping {
  group: string;
  role: SsoRole;
}

interface OidcForm {
  issuer: string;
  discoveryUrl: string;
  clientId: string;
  /** New secret to save; empty means "keep the previously-saved one". */
  clientSecret: string;
  scopes: string;
  emailClaim: string;
  nameClaim: string;
  groupsClaim: string;
  defaultRole: SsoRole;
  groupRoleMap: GroupRoleMapping[];
  enabled: boolean;
  jitProvisioning: boolean;
}

function configToOidcForm(config: OidcConfig | null): OidcForm {
  if (!config) {
    return {
      issuer: "",
      discoveryUrl: "",
      clientId: "",
      clientSecret: "",
      scopes: DEFAULT_OIDC_SCOPES,
      emailClaim: "email",
      nameClaim: "name",
      groupsClaim: "groups",
      defaultRole: "member",
      groupRoleMap: [],
      enabled: false,
      jitProvisioning: true,
    };
  }
  const role = (SSO_ROLES as readonly string[]).includes(config.default_role)
    ? (config.default_role as SsoRole)
    : "member";
  return {
    issuer: config.issuer,
    discoveryUrl: config.discovery_url ?? "",
    clientId: config.client_id,
    clientSecret: "",
    scopes: config.scopes.join(", "),
    emailClaim: config.email_claim,
    nameClaim: config.name_claim,
    groupsClaim: config.groups_claim,
    defaultRole: role,
    groupRoleMap: Object.entries(config.group_role_map).map(
      ([group, mappedRole]) => ({
        group,
        role: (SSO_ROLES as readonly string[]).includes(mappedRole)
          ? (mappedRole as SsoRole)
          : "member",
      }),
    ),
    enabled: config.enabled,
    jitProvisioning: config.jit_provisioning,
  };
}

export interface SsoSettingsViewProps {
  workspaceId?: string;
  client?: ForgeApiClient;
}

/**
 * SSO / SCIM settings (F33) — the workspace's identity-federation control plane.
 * The screen is organised around the trust link between the customer's identity
 * provider and Forge (the service provider): a signature header renders that link
 * and its live/paused/unlinked state, the master switch flips it, and the cards
 * below configure the IdP, expose the SP details to hand back, verify login
 * domains, probe home-realm discovery, and manage SCIM provisioning tokens.
 * Ember is spent only on Save; the "live" state speaks in success green.
 */
export function SsoSettingsView({
  workspaceId = DEFAULT_WORKSPACE_ID,
  client = apiClient,
}: SsoSettingsViewProps) {
  const configQuery = useSsoConfig(workspaceId, client);
  const config = configQuery.data ?? null;

  const put = usePutSsoConfig(client);
  const setEnabled = useSetSsoEnabled(client);

  const [form, setForm] = useState<SamlForm>(() => configToForm(null));
  const [seededId, setSeededId] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);
  const [domainDraft, setDomainDraft] = useState("");

  // Seed the form from the loaded config exactly once per config identity, so an
  // unrelated background refetch never clobbers an in-progress edit.
  const configId = config?.id ?? null;
  if (configId !== seededId) {
    setSeededId(configId);
    setForm(configToForm(config));
    setSaveError(null);
  }

  const patch = useCallback(
    <K extends keyof SamlForm>(key: K, value: SamlForm[K]) =>
      setForm((prev) => ({ ...prev, [key]: value })),
    [],
  );

  const dirty = useMemo(
    () => JSON.stringify(form) !== JSON.stringify(configToForm(config)),
    [form, config],
  );

  const canSave =
    form.idpEntityId.trim().length > 0 &&
    form.idpSsoUrl.trim().length > 0 &&
    form.idpCert.trim().length > 0 &&
    !put.isPending;

  const save = useCallback(() => {
    if (
      !form.idpEntityId.trim() ||
      !form.idpSsoUrl.trim() ||
      !form.idpCert.trim() ||
      put.isPending
    ) {
      return;
    }
    setSaveError(null);
    put.mutate(
      {
        workspaceId,
        body: {
          protocol: "saml",
          enabled: config?.enabled ?? false,
          idp: {
            entity_id: form.idpEntityId.trim(),
            sso_url: form.idpSsoUrl.trim(),
            slo_url: form.idpSloUrl.trim() || null,
            x509_certs: [form.idpCert.trim()],
            name_id_format: form.nameIdFormat,
          },
          domains: form.domains,
          allow_idp_initiated: form.allowIdpInitiated,
          sign_authn_requests: form.signAuthnRequests,
          want_assertions_signed: form.wantAssertionsSigned,
          attribute_mapping: config?.attribute_mapping ?? { email: "" },
          group_role_map: config?.group_role_map ?? {},
          default_role: form.defaultRole,
          jit_provisioning: form.jitProvisioning,
        },
      },
      {
        onSuccess: () => toast.success("SSO configuration saved."),
        onError: (err) => setSaveError(saveErrorMessage(err)),
      },
    );
  }, [form, config, put, workspaceId]);

  const onToggleEnabled = useCallback(
    (next: boolean) => {
      if (!config || setEnabled.isPending) return;
      setToggleError(null);
      setEnabled.mutate(
        { workspaceId, enabled: next },
        {
          onSuccess: () =>
            toast.success(next ? "SSO enabled." : "SSO disabled."),
          onError: (err) => setToggleError(saveErrorMessage(err)),
        },
      );
    },
    [config, setEnabled, workspaceId],
  );

  const addDomain = useCallback(() => {
    const domain = normalizeDomain(domainDraft);
    if (!isValidDomain(domain)) return;
    setForm((prev) =>
      prev.domains.includes(domain)
        ? prev
        : { ...prev, domains: [...prev.domains, domain].sort() },
    );
    setDomainDraft("");
  }, [domainDraft]);

  const removeDomain = useCallback(
    (domain: string) =>
      setForm((prev) => ({
        ...prev,
        domains: prev.domains.filter((d) => d !== domain),
      })),
    [],
  );

  // --- OIDC: a second, independent identity-provider configuration ---------- //
  const [protocol, setProtocol] = useState<SsoProtocol>("saml");

  const oidcConfigQuery = useOidcConfig(workspaceId, client);
  const oidcConfig = oidcConfigQuery.data ?? null;
  const putOidc = usePutOidcConfig(client);

  const [oidcForm, setOidcForm] = useState<OidcForm>(() =>
    configToOidcForm(null),
  );
  const [oidcSeededId, setOidcSeededId] = useState<string | null>(null);
  const [oidcSaveError, setOidcSaveError] = useState<string | null>(null);

  const oidcConfigId = oidcConfig?.id ?? null;
  if (oidcConfigId !== oidcSeededId) {
    setOidcSeededId(oidcConfigId);
    setOidcForm(configToOidcForm(oidcConfig));
    setOidcSaveError(null);
  }

  const patchOidc = useCallback(
    <K extends keyof OidcForm>(key: K, value: OidcForm[K]) =>
      setOidcForm((prev) => ({ ...prev, [key]: value })),
    [],
  );

  const oidcDirty = useMemo(
    () => JSON.stringify(oidcForm) !== JSON.stringify(configToOidcForm(oidcConfig)),
    [oidcForm, oidcConfig],
  );

  const oidcCanSave =
    oidcForm.issuer.trim().length > 0 &&
    oidcForm.clientId.trim().length > 0 &&
    (Boolean(oidcConfig) || oidcForm.clientSecret.trim().length > 0) &&
    !putOidc.isPending;

  const saveOidc = useCallback(() => {
    if (
      !oidcForm.issuer.trim() ||
      !oidcForm.clientId.trim() ||
      (!oidcConfig && !oidcForm.clientSecret.trim()) ||
      putOidc.isPending
    ) {
      return;
    }
    setOidcSaveError(null);
    const scopes = oidcForm.scopes
      .split(/[,\s]+/)
      .map((scope) => scope.trim())
      .filter(Boolean);
    const groupRoleMap = Object.fromEntries(
      oidcForm.groupRoleMap
        .filter((entry) => entry.group.trim().length > 0)
        .map((entry) => [entry.group.trim(), entry.role]),
    );
    putOidc.mutate(
      {
        workspaceId,
        body: {
          enabled: oidcForm.enabled,
          issuer: oidcForm.issuer.trim(),
          discovery_url: oidcForm.discoveryUrl.trim() || null,
          client_id: oidcForm.clientId.trim(),
          client_secret: oidcForm.clientSecret.trim() || null,
          scopes: scopes.length > 0 ? scopes : undefined,
          email_claim: oidcForm.emailClaim.trim() || "email",
          name_claim: oidcForm.nameClaim.trim() || "name",
          groups_claim: oidcForm.groupsClaim.trim() || "groups",
          default_role: oidcForm.defaultRole,
          group_role_map: groupRoleMap,
          jit_provisioning: oidcForm.jitProvisioning,
        },
      },
      {
        onSuccess: () => toast.success("OIDC configuration saved."),
        onError: (err) => setOidcSaveError(saveErrorMessage(err)),
      },
    );
  }, [oidcForm, oidcConfig, putOidc, workspaceId]);

  const addGroupRoleMapping = useCallback(() => {
    setOidcForm((prev) => ({
      ...prev,
      groupRoleMap: [...prev.groupRoleMap, { group: "", role: "member" }],
    }));
  }, []);

  const updateGroupRoleMapping = useCallback(
    (index: number, patchValue: Partial<GroupRoleMapping>) => {
      setOidcForm((prev) => ({
        ...prev,
        groupRoleMap: prev.groupRoleMap.map((entry, i) =>
          i === index ? { ...entry, ...patchValue } : entry,
        ),
      }));
    },
    [],
  );

  const removeGroupRoleMapping = useCallback((index: number) => {
    setOidcForm((prev) => ({
      ...prev,
      groupRoleMap: prev.groupRoleMap.filter((_, i) => i !== index),
    }));
  }, []);

  // Save via the command palette (keyboard-first).
  const saveRef = useRef(save);
  useEffect(() => {
    saveRef.current = save;
  }, [save]);
  const commands = useMemo(
    () => [
      {
        id: "sso-save",
        label: "Save SSO configuration",
        group: "Settings",
        icon: <Save />,
        run: () => saveRef.current(),
      },
    ],
    [],
  );
  useRegisterCommands("sso-settings", commands);

  if (configQuery.isLoading) {
    return <ScreenSkeleton />;
  }
  if (configQuery.isError) {
    return <ScreenError onRetry={() => configQuery.refetch()} />;
  }

  const state = federationState(config);

  return (
    <div
      data-testid="sso-view"
      className="mx-auto flex w-full max-w-4xl flex-col gap-6"
    >
      {/* Signature: the IdP <-> Forge trust link + master switch */}
      <header className="flex flex-col gap-5 rounded-xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
              <Fingerprint className="h-5 w-5" aria-hidden />
            </span>
            <div>
              <h1 className="font-display text-xl font-semibold tracking-tight">
                Single sign-on
              </h1>
              <p className="text-sm text-muted-foreground">
                SAML federation and SCIM provisioning for your workspace.
              </p>
            </div>
          </div>
          <div className="flex flex-col items-end gap-1">
            <SsoSwitch
              tone="success"
              checked={config?.enabled ?? false}
              disabled={!config || setEnabled.isPending}
              onCheckedChange={onToggleEnabled}
              label={config?.enabled ? "SSO enabled" : "SSO disabled"}
              description={
                config
                  ? "Members sign in through your IdP"
                  : "Save a configuration first"
              }
            />
            {toggleError ? (
              <p role="alert" className="max-w-xs text-right text-xs text-danger">
                {toggleError}
              </p>
            ) : null}
          </div>
        </div>

        <TrustLink state={state} config={config} idpEntityId={form.idpEntityId} />
      </header>

      {/* Identity provider (SAML or OIDC) */}
      <Card
        icon={<PlugZap className="h-5 w-5" aria-hidden />}
        title="Identity provider"
        description={
          protocol === "oidc"
            ? "The OpenID Connect details Forge uses to trust and validate your IdP."
            : "The SAML details Forge uses to trust and validate your IdP."
        }
      >
        {protocol === "saml" && !config ? (
          <div
            data-testid="sso-onboarding"
            className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
          >
            Not configured yet. Enter your IdP&apos;s SAML details and save to
            establish the trust link — you can enable SSO once it&apos;s in place.
          </div>
        ) : null}
        {protocol === "oidc" && !oidcConfig ? (
          <div
            data-testid="oidc-onboarding"
            className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
          >
            Not configured yet. Enter your IdP&apos;s OpenID Connect details and
            save to establish the trust link — you can enable OIDC sign-in once
            it&apos;s in place.
          </div>
        ) : null}

        <ProtocolPicker value={protocol} onChange={setProtocol} />

        {protocol === "saml" ? (
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              save();
            }}
            className="flex flex-col gap-4"
          >
            <Field label="IdP Entity ID" required>
              <input
                value={form.idpEntityId}
                onChange={(e) => patch("idpEntityId", e.target.value)}
                placeholder="https://idp.example.com/saml/metadata"
                className={cn(FIELD, "font-mono text-xs")}
              />
            </Field>
  
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="IdP SSO URL" required>
                <input
                  value={form.idpSsoUrl}
                  onChange={(e) => patch("idpSsoUrl", e.target.value)}
                  placeholder="https://idp.example.com/sso"
                  className={cn(FIELD, "font-mono text-xs")}
                />
              </Field>
              <Field label="IdP SLO URL" hint="not yet supported">
                <input
                  value={form.idpSloUrl}
                  disabled
                  aria-describedby="idp-slo-help"
                  onChange={(e) => patch("idpSloUrl", e.target.value)}
                  placeholder="https://idp.example.com/slo"
                  className={cn(
                    FIELD,
                    "font-mono text-xs disabled:cursor-not-allowed disabled:opacity-60",
                  )}
                />
                <p
                  id="idp-slo-help"
                  className="text-xs text-muted-foreground"
                >
                  Single Logout is not yet supported. Forge keeps any saved
                  value but can&apos;t yet act on it during sign-out.
                </p>
              </Field>
            </div>
  
            <Field
              label="IdP signing certificate"
              required
              hint={
                countCerts(form.idpCert) > 0
                  ? `${countCerts(form.idpCert)} PEM block${countCerts(form.idpCert) === 1 ? "" : "s"}`
                  : "PEM"
              }
            >
              <textarea
                value={form.idpCert}
                onChange={(e) => patch("idpCert", e.target.value)}
                placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"}
                rows={4}
                className={cn(FIELD, "resize-y font-mono text-[11px] leading-relaxed")}
              />
            </Field>
  
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="NameID format">
                <select
                  value={form.nameIdFormat}
                  onChange={(e) => patch("nameIdFormat", e.target.value)}
                  className={FIELD}
                >
                  {NAMEID_FORMATS.map((f) => (
                    <option key={f.value} value={f.value}>
                      {f.label}
                    </option>
                  ))}
                  {NAMEID_FORMATS.every((f) => f.value !== form.nameIdFormat) ? (
                    <option value={form.nameIdFormat}>
                      {nameIdFormatLabel(form.nameIdFormat)}
                    </option>
                  ) : null}
                </select>
              </Field>
              <Field label="Default role" hint="for JIT-provisioned users">
                <select
                  value={form.defaultRole}
                  onChange={(e) => patch("defaultRole", e.target.value as SsoRole)}
                  className={FIELD}
                >
                  {SSO_ROLES.map((role) => (
                    <option key={role} value={role}>
                      {ROLE_LABELS[role]}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
  
            <div className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Security
              </h3>
              <SsoSwitch
                checked={form.signAuthnRequests}
                onCheckedChange={(v) => patch("signAuthnRequests", v)}
                label="Sign authentication requests"
                description="Forge signs the AuthnRequest it sends to your IdP."
              />
              <SsoSwitch
                checked={form.wantAssertionsSigned}
                onCheckedChange={(v) => patch("wantAssertionsSigned", v)}
                label="Require signed assertions"
                description="Reject any SAML response whose assertion isn't signed."
              />
              <SsoSwitch
                checked={form.allowIdpInitiated}
                onCheckedChange={(v) => patch("allowIdpInitiated", v)}
                label="Allow IdP-initiated login"
                description="Accept logins started from your IdP's app dashboard."
              />
              <SsoSwitch
                checked={form.jitProvisioning}
                onCheckedChange={(v) => patch("jitProvisioning", v)}
                label="Just-in-time provisioning"
                description="Create a Forge account on a user's first SSO login."
              />
            </div>
  
            {saveError ? (
              <p role="alert" className="text-sm text-danger">
                {saveError}
              </p>
            ) : null}
  
            <div className="flex items-center justify-between gap-3">
              <span
                data-testid="sso-dirty"
                role="status"
                aria-live="polite"
                className="text-xs text-muted-foreground"
              >
                {put.isPending
                  ? "Saving…"
                  : dirty
                    ? "Unsaved changes"
                    : config
                      ? "All changes saved"
                      : ""}
              </span>
              <button
                type="submit"
                data-testid="sso-save"
                disabled={!canSave}
                className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <Save className="h-4 w-4" aria-hidden />
                {config ? "Save configuration" : "Save & establish trust"}
              </button>
            </div>
          </form>
        ) : (
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              saveOidc();
            }}
            className="flex flex-col gap-4"
          >
            <Field label="Issuer" required hint="the OIDC issuer identifier">
              <input
                value={oidcForm.issuer}
                onChange={(e) => patchOidc("issuer", e.target.value)}
                placeholder="https://idp.example.com"
                className={cn(FIELD, "font-mono text-xs")}
              />
            </Field>
  
            <Field
              label="Discovery URL"
              hint="optional — defaults to {issuer}/.well-known/openid-configuration"
            >
              <input
                value={oidcForm.discoveryUrl}
                onChange={(e) => patchOidc("discoveryUrl", e.target.value)}
                placeholder="https://idp.example.com/.well-known/openid-configuration"
                className={cn(FIELD, "font-mono text-xs")}
              />
            </Field>
  
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Client ID" required>
                <input
                  value={oidcForm.clientId}
                  onChange={(e) => patchOidc("clientId", e.target.value)}
                  placeholder="forge-oidc-client"
                  className={cn(FIELD, "font-mono text-xs")}
                />
              </Field>
              <Field
                label="Client secret"
                required={!oidcConfig}
                hint={oidcConfig ? "leave blank to keep the saved secret" : "write-only"}
              >
                <input
                  type="password"
                  autoComplete="new-password"
                  value={oidcForm.clientSecret}
                  onChange={(e) => patchOidc("clientSecret", e.target.value)}
                  placeholder={oidcConfig ? "••••••••" : "client secret"}
                  className={cn(FIELD, "font-mono text-xs")}
                />
              </Field>
            </div>
  
            <Field label="Scopes" hint="space or comma separated">
              <input
                value={oidcForm.scopes}
                onChange={(e) => patchOidc("scopes", e.target.value)}
                placeholder={DEFAULT_OIDC_SCOPES}
                className={cn(FIELD, "font-mono text-xs")}
              />
            </Field>
  
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Email claim">
                <input
                  value={oidcForm.emailClaim}
                  onChange={(e) => patchOidc("emailClaim", e.target.value)}
                  className={cn(FIELD, "font-mono text-xs")}
                />
              </Field>
              <Field label="Name claim">
                <input
                  value={oidcForm.nameClaim}
                  onChange={(e) => patchOidc("nameClaim", e.target.value)}
                  className={cn(FIELD, "font-mono text-xs")}
                />
              </Field>
              <Field label="Groups claim">
                <input
                  value={oidcForm.groupsClaim}
                  onChange={(e) => patchOidc("groupsClaim", e.target.value)}
                  className={cn(FIELD, "font-mono text-xs")}
                />
              </Field>
            </div>
  
            <Field label="Default role" hint="for JIT-provisioned users">
              <select
                value={oidcForm.defaultRole}
                onChange={(e) => patchOidc("defaultRole", e.target.value as SsoRole)}
                className={FIELD}
              >
                {SSO_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {ROLE_LABELS[role]}
                  </option>
                ))}
              </select>
            </Field>
  
            <div className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Access
              </h3>
              <SsoSwitch
                checked={oidcForm.enabled}
                onCheckedChange={(v) => patchOidc("enabled", v)}
                label="Enable OIDC sign-in"
                description="Allow members to sign in through this identity provider."
              />
              <SsoSwitch
                checked={oidcForm.jitProvisioning}
                onCheckedChange={(v) => patchOidc("jitProvisioning", v)}
                label="Just-in-time provisioning"
                description="Create a Forge account on a user's first OIDC login."
              />
            </div>
  
            <div className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Role mapping
                </h3>
                <button
                  type="button"
                  data-testid="oidc-add-mapping"
                  onClick={addGroupRoleMapping}
                  className="inline-flex h-7 items-center rounded-md border border-border px-2 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Add mapping
                </button>
              </div>
              <p className="text-xs text-muted-foreground">
                Grant a role automatically when the groups claim contains a
                matching IdP group. Unmapped users get the default role above.
              </p>
              {oidcForm.groupRoleMap.length === 0 ? (
                <p
                  data-testid="role-map-empty"
                  className="text-xs text-muted-foreground"
                >
                  No group mappings yet.
                </p>
              ) : (
                <ul data-testid="role-map-list" className="flex flex-col gap-2">
                  {oidcForm.groupRoleMap.map((entry, index) => (
                    <li key={index} className="flex items-center gap-2">
                      <label className="sr-only" htmlFor={`role-map-group-${index}`}>
                        Group name
                      </label>
                      <input
                        id={`role-map-group-${index}`}
                        value={entry.group}
                        onChange={(e) =>
                          updateGroupRoleMapping(index, { group: e.target.value })
                        }
                        placeholder="forge-admins"
                        className={cn(FIELD, "font-mono text-xs")}
                      />
                      <select
                        aria-label={`Role for mapping ${index + 1}`}
                        value={entry.role}
                        onChange={(e) =>
                          updateGroupRoleMapping(index, {
                            role: e.target.value as SsoRole,
                          })
                        }
                        className={cn(FIELD, "max-w-[10rem]")}
                      >
                        {SSO_ROLES.map((role) => (
                          <option key={role} value={role}>
                            {ROLE_LABELS[role]}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        aria-label={`Remove mapping ${index + 1}`}
                        onClick={() => removeGroupRoleMapping(index)}
                        className="rounded-full text-muted-foreground transition-colors hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <X className="h-3.5 w-3.5" aria-hidden />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
  
            {oidcSaveError ? (
              <p role="alert" className="text-sm text-danger">
                {oidcSaveError}
              </p>
            ) : null}
  
            <div className="flex items-center justify-between gap-3">
              <span
                data-testid="oidc-dirty"
                role="status"
                aria-live="polite"
                className="text-xs text-muted-foreground"
              >
                {putOidc.isPending
                  ? "Saving…"
                  : oidcDirty
                    ? "Unsaved changes"
                    : oidcConfig
                      ? "All changes saved"
                      : ""}
              </span>
              <button
                type="submit"
                data-testid="oidc-save"
                disabled={!oidcCanSave}
                className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <Save className="h-4 w-4" aria-hidden />
                {oidcConfig ? "Save configuration" : "Save & establish trust"}
              </button>
            </div>
          </form>
        )}
      </Card>

      {/* Service provider (Forge) — what the admin pastes back into the IdP */}
      <Card
        icon={<ServerCog className="h-5 w-5" aria-hidden />}
        title="Service provider"
        description={
          protocol === "oidc"
            ? "Forge's OIDC details. Hand these back to your IdP to complete the trust."
            : "Forge's SAML details. Hand these back to your IdP to complete the trust."
        }
      >
        {protocol === "oidc" ? (
          oidcConfig ? (
            <div className="flex flex-col gap-4">
              <CopyField label="Redirect URI" value={oidcConfig.redirect_uri} />
              <CopyField label="Login URL" value={oidcConfig.login_url} />
            </div>
          ) : (
            <p
              data-testid="oidc-sp-pending"
              className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-6 text-center text-sm text-muted-foreground"
            >
              Forge generates its redirect URI once you save your first OIDC
              configuration.
            </p>
          )
        ) : config ? (
          <div className="flex flex-col gap-4">
            <CopyField label="SP Entity ID" value={config.sp_entity_id} />
            <CopyField label="ACS URL" value={config.sp_acs_url} />
            <CopyField label="Metadata URL" value={config.sp_metadata_url} />
            <CopyField
              label="SP certificate"
              value={config.sp_cert_pem}
              multiline
            />
          </div>
        ) : (
          <p
            data-testid="sp-pending"
            className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-6 text-center text-sm text-muted-foreground"
          >
            Forge generates its SP entity ID, ACS URL and signing certificate when
            you save your first configuration.
          </p>
        )}
      </Card>

      {/* Domains + home-realm discovery */}
      <Card
        icon={<Globe className="h-5 w-5" aria-hidden />}
        title="Verified domains"
        description="Email domains that route to this workspace's SSO on sign-in."
      >
        <div className="flex flex-col gap-3">
          {form.domains.length === 0 ? (
            <p
              data-testid="domains-empty"
              className="text-sm text-muted-foreground"
            >
              No domains yet. Add the email domains your team signs in with (e.g.{" "}
              <span className="font-mono text-xs">acme.com</span>).
            </p>
          ) : (
            <ul className="flex flex-wrap gap-2" data-testid="domain-list">
              {form.domains.map((domain) => (
                <li
                  key={domain}
                  className="inline-flex items-center gap-1.5 rounded-full border border-success/40 bg-success/10 px-2.5 py-1 text-sm text-foreground"
                >
                  <ShieldCheck
                    className="h-3.5 w-3.5 text-success"
                    aria-hidden
                  />
                  <span className="font-mono text-xs">{domain}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${domain}`}
                    onClick={() => removeDomain(domain)}
                    className="rounded-full text-muted-foreground transition-colors hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <X className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="flex items-center gap-2">
            <label htmlFor="sso-add-domain" className="sr-only">
              Add domain
            </label>
            <input
              id="sso-add-domain"
              value={domainDraft}
              onChange={(e) => setDomainDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addDomain();
                }
              }}
              placeholder="acme.com"
              className={cn(FIELD, "max-w-xs font-mono text-xs")}
            />
            <button
              type="button"
              onClick={addDomain}
              disabled={!isValidDomain(domainDraft)}
              className="inline-flex h-9 shrink-0 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
            >
              Add domain
            </button>
          </div>
          <p className="text-xs text-muted-foreground">
            Domains take effect on save. Each is admin-verified and must be unique
            across workspaces.
          </p>

          <HrdProbe client={client} />
        </div>
      </Card>

      {/* SCIM provisioning */}
      <ScimPanel
        workspaceId={workspaceId}
        config={config}
        apiBaseUrl={client.baseUrl}
        client={client}
      />
    </div>
  );
}

// --- Home-realm discovery probe ------------------------------------------- //

function HrdProbe({ client }: { client: ForgeApiClient }) {
  const discover = useDiscoverSso(client);
  const [email, setEmail] = useState("");
  const result = discover.data;

  const onTest = () => {
    const value = email.trim();
    if (!value || discover.isPending) return;
    discover.mutate(value);
  };

  return (
    <div className="mt-1 flex flex-col gap-2 rounded-lg border border-border bg-muted/30 p-4">
      <div className="flex items-center gap-2">
        <Link2 className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h3 className="text-sm font-medium text-foreground">
          Test home-realm discovery
        </h3>
      </div>
      <p className="text-xs text-muted-foreground">
        Check where a sign-in email routes — SSO or password login.
      </p>
      <div className="flex items-center gap-2">
        <label htmlFor="hrd-email" className="sr-only">
          Test login email
        </label>
        <input
          id="hrd-email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onTest();
            }
          }}
          placeholder="user@acme.com"
          className={cn(FIELD, "max-w-xs")}
        />
        <button
          type="button"
          data-testid="hrd-test"
          onClick={onTest}
          disabled={!email.trim() || discover.isPending}
          className="inline-flex h-9 shrink-0 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
        >
          {discover.isPending ? "Checking…" : "Test"}
        </button>
      </div>
      {discover.isError ? (
        <p role="alert" className="text-xs text-danger">
          Couldn&apos;t check that email. Please try again.
        </p>
      ) : result ? (
        <div
          data-testid="hrd-result"
          className={cn(
            "flex items-start gap-2 rounded-md border px-3 py-2 text-xs",
            result.sso
              ? "border-success/40 bg-success/10 text-success"
              : "border-border bg-muted text-muted-foreground",
          )}
        >
          {result.sso ? (
            <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          ) : (
            <Link2Off className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          )}
          <span>
            {result.sso
              ? "Routes to SSO. This email signs in through your identity provider."
              : "No SSO for this domain. This email uses password login."}
          </span>
        </div>
      ) : null}
    </div>
  );
}

// --- Trust-link signature -------------------------------------------------- //

interface TrustLinkProps {
  state: ReturnType<typeof federationState>;
  config: SsoConfig | null;
  idpEntityId: string;
}

function TrustLink({ state, config, idpEntityId }: TrustLinkProps) {
  const idpLabel = hostLabel(idpEntityId) || "Identity provider";
  const spLabel = config ? hostLabel(config.sp_entity_id) : "Forge";
  const linked = state !== "unlinked";

  const stateStyle =
    state === "established"
      ? "border-success/40 bg-success/10 text-success"
      : state === "paused"
        ? "border-warning/40 bg-warning/10 text-warning"
        : "border-dashed border-border bg-muted text-muted-foreground";
  const stateLabel =
    state === "established"
      ? "Trust established"
      : state === "paused"
        ? "Trust paused"
        : "Not linked";
  const lineStyle =
    state === "established"
      ? "bg-success/50"
      : state === "paused"
        ? "bg-warning/50"
        : "bg-border";

  return (
    <div
      data-testid="trust-link"
      className="grid gap-3 sm:grid-cols-[1fr_auto_1fr] sm:items-center"
    >
      <TrustNode
        testid="trust-idp"
        icon={<Building2 className="h-4 w-4" aria-hidden />}
        title={idpLabel}
        subtitle="Your identity provider"
        muted={!idpEntityId}
      />
      <div className="flex items-center justify-center gap-2">
        <span
          className={cn("hidden h-px w-6 sm:block", lineStyle)}
          aria-hidden
        />
        <span
          data-testid="trust-state"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
            stateStyle,
          )}
        >
          {linked ? (
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          ) : (
            <Link2Off className="h-3.5 w-3.5" aria-hidden />
          )}
          {stateLabel}
        </span>
        <span
          className={cn("hidden h-px w-6 sm:block", lineStyle)}
          aria-hidden
        />
      </div>
      <TrustNode
        testid="trust-sp"
        icon={<ShieldCheck className="h-4 w-4" aria-hidden />}
        title={spLabel}
        subtitle="Forge (service provider)"
        muted={!config}
      />
    </div>
  );
}

function TrustNode({
  icon,
  title,
  subtitle,
  muted,
  testid,
}: {
  icon: ReactNode;
  title: string;
  subtitle: string;
  muted: boolean;
  testid: string;
}) {
  return (
    <div
      data-testid={testid}
      className={cn(
        "flex items-center gap-3 rounded-lg border px-4 py-3",
        muted ? "border-dashed border-border bg-muted/20" : "border-border bg-muted/40",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-background text-muted-foreground">
        {icon}
      </span>
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-foreground">{title}</p>
        <p className="truncate text-[11px] text-muted-foreground">{subtitle}</p>
      </div>
    </div>
  );
}

// --- Layout primitives ----------------------------------------------------- //

function Card({
  icon,
  title,
  description,
  children,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
          {icon}
        </span>
        <div>
          <h2 className="font-display text-base font-semibold tracking-tight">
            {title}
          </h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-foreground">
          {label}
          {required ? (
            <span className="ml-0.5 text-danger" aria-hidden>
              *
            </span>
          ) : null}
        </span>
        {hint ? (
          <span className="text-[11px] text-muted-foreground">{hint}</span>
        ) : null}
      </span>
      {children}
    </label>
  );
}

function ProtocolPicker({
  value,
  onChange,
}: {
  value: SsoProtocol;
  onChange: (protocol: SsoProtocol) => void;
}) {
  return (
    <div
      className="inline-flex items-center gap-1 self-start rounded-md border border-border bg-muted/40 p-0.5 text-sm"
      role="group"
      aria-label="SSO protocol"
    >
      <button
        type="button"
        data-testid="protocol-saml"
        aria-pressed={value === "saml"}
        onClick={() => onChange("saml")}
        className={cn(
          "inline-flex items-center gap-1.5 rounded px-3 py-1 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          value === "saml"
            ? "bg-background text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        <KeyRound className="h-3.5 w-3.5 text-primary" aria-hidden />
        SAML 2.0
      </button>
      <button
        type="button"
        data-testid="protocol-oidc"
        aria-pressed={value === "oidc"}
        onClick={() => onChange("oidc")}
        className={cn(
          "inline-flex items-center gap-1.5 rounded px-3 py-1 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          value === "oidc"
            ? "bg-background text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        <Globe className="h-3.5 w-3.5 text-primary" aria-hidden />
        OIDC
      </button>
    </div>
  );
}

// --- Top-level states ------------------------------------------------------ //

function ScreenSkeleton() {
  return (
    <div
      data-testid="sso-skeleton"
      aria-busy="true"
      className="mx-auto flex w-full max-w-4xl flex-col gap-6"
    >
      <div className="h-32 animate-pulse rounded-xl border border-border bg-card" />
      <div className="h-80 animate-pulse rounded-xl border border-border bg-card" />
      <div className="h-48 animate-pulse rounded-xl border border-border bg-card" />
    </div>
  );
}

function ScreenError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="sso-error"
      role="status"
      className="mx-auto flex w-full max-w-4xl flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border px-6 py-16 text-center"
    >
      <ServerCog className="h-8 w-8 text-muted-foreground" aria-hidden />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">
          SSO settings unavailable
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          The identity service is unreachable. Your configuration is safe — try
          again in a moment.
        </p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Retry
      </button>
    </div>
  );
}
