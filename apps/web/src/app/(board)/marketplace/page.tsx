import { MarketplaceView } from "@/components/marketplace/marketplace-view";

/**
 * Marketplace (F32) — browse/search community skill profiles and MCP
 * connectors, inspect each package's manifest and verification provenance, and
 * install/update it. Backed by the typed `/marketplace` router.
 */
export default function MarketplacePage() {
  return <MarketplaceView />;
}
