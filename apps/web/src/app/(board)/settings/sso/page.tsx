import { SsoSettingsView } from "@/components/sso/sso-settings-view";

/**
 * SSO / SCIM settings (F33) — the workspace's identity-federation control plane.
 * A signature trust-link header (your IdP <-> SCF) with the master enable
 * switch, above cards to configure the SAML IdP, hand back SCF's SP details,
 * verify login domains, probe home-realm discovery, and manage SCIM provisioning
 * tokens. Backed by the typed `/workspaces/{id}/sso` + `/scim/tokens` +
 * `/auth/saml/discover` routers; ember is reserved for the single Save action.
 */
export default function SsoSettingsPage() {
  return <SsoSettingsView />;
}
