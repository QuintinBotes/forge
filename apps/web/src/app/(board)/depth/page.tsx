import { BoardDepth } from "@/components/board/depth/board-depth";

/**
 * Board depth — the elevated board surface: status-rule-aware Kanban with
 * drag-to-move, a roadmap timeline, saved filters, multi-select bulk actions and
 * a deep Cmd+K palette. Backed by the typed board API with optimistic mutations.
 */
export default function BoardDepthPage() {
  return <BoardDepth initialView="board" />;
}
