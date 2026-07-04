import { describe, expect, it } from "vitest";

import type { TaskDTO } from "@/lib/api/types";
import {
  PRIORITY_LABELS,
  STATUS_COLUMNS,
  STATUS_LABELS,
  groupByStatus,
  nextStatus,
  prevStatus,
} from "./status";

describe("board status helpers", () => {
  it("exposes every TaskStatus as an ordered column with a label", () => {
    expect(STATUS_COLUMNS[0]).toBe("backlog");
    expect(STATUS_COLUMNS).toContain("in_progress");
    expect(STATUS_COLUMNS).toContain("done");
    for (const status of STATUS_COLUMNS) {
      expect(STATUS_LABELS[status]).toBeTruthy();
    }
  });

  it("maps every priority to a human label", () => {
    expect(PRIORITY_LABELS.urgent).toBe("Urgent");
    expect(PRIORITY_LABELS.low).toBe("Low");
  });

  it("advances and rewinds along the workflow order", () => {
    expect(nextStatus("backlog")).toBe("ready");
    expect(prevStatus("ready")).toBe("backlog");
  });

  it("returns null at the ends of the workflow", () => {
    expect(prevStatus(STATUS_COLUMNS[0])).toBeNull();
    expect(nextStatus(STATUS_COLUMNS[STATUS_COLUMNS.length - 1])).toBeNull();
  });

  it("groups tasks by status, defaulting missing status to backlog", () => {
    const tasks: TaskDTO[] = [
      { id: "a", title: "A", status: "in_progress" },
      { id: "b", title: "B", status: "done" },
      { id: "c", title: "C" }, // no status -> backlog
    ];
    const grouped = groupByStatus(tasks);
    expect(grouped.in_progress.map((t) => t.id)).toEqual(["a"]);
    expect(grouped.done.map((t) => t.id)).toEqual(["b"]);
    expect(grouped.backlog.map((t) => t.id)).toEqual(["c"]);
    // Every column exists as a key even when empty.
    for (const status of STATUS_COLUMNS) {
      expect(Array.isArray(grouped[status])).toBe(true);
    }
  });
});
