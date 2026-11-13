import { KnowledgeView } from "@/components/knowledge/knowledge-view";

/**
 * Knowledge base — the indexed sources with their freshness and embedding
 * coverage, a search that states which retrieval legs ran, and whole
 * documents as plain text.
 */
export default function KnowledgePage() {
  return <KnowledgeView />;
}
