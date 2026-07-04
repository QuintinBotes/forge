import { BoardView } from "@/components/board/board-view";

/**
 * Board home — List view. Backed by the typed API client with optimistic status
 * changes, realtime invalidation, and the Cmd+K command palette.
 */
export default function BoardHomePage() {
  return <BoardView initialView="list" />;
}
