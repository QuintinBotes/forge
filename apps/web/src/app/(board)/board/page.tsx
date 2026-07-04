import { BoardView } from "@/components/board/board-view";

/** Kanban view — columns per status with optimistic drag-free move controls. */
export default function KanbanPage() {
  return <BoardView initialView="board" />;
}
