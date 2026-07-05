import { afterEach, describe, expect, it } from "vitest";

import type { TaskDTO } from "@/lib/api/types";
import {
  ALL_VIEW,
  PRESET_VIEWS,
  SAVED_VIEWS_KEY,
  filterTasks,
  loadSavedViews,
  matchesQuery,
  matchesView,
  persistSavedViews,
  type BoardView,
} from "./filters";

const task = (overrides: Partial<TaskDTO>): TaskDTO => ({
  title: "Task",
  ...overrides,
});

describe("matchesView", () => {
  it("matches everything for the all view", () => {
    expect(matchesView(task({ status: "done" }), ALL_VIEW)).toBe(true);
  });

  it("filters by status", () => {
    const view: BoardView = { id: "v", label: "v", statuses: ["blocked"] };
    expect(matchesView(task({ status: "blocked" }), view)).toBe(true);
    expect(matchesView(task({ status: "in_progress" }), view)).toBe(false);
  });

  it("filters by priority", () => {
    const view: BoardView = { id: "v", label: "v", priorities: ["high", "urgent"] };
    expect(matchesView(task({ priority: "urgent" }), view)).toBe(true);
    expect(matchesView(task({ priority: "low" }), view)).toBe(false);
    expect(matchesView(task({}), view)).toBe(false);
  });

  it("resolves the me / unassigned assignment axes", () => {
    const mine: BoardView = { id: "m", label: "m", assignee: "me" };
    expect(matchesView(task({ assignee_id: "u1" }), mine, { currentUserId: "u1" })).toBe(true);
    expect(matchesView(task({ assignee_id: "u2" }), mine, { currentUserId: "u1" })).toBe(false);
    // Unknown viewer never matches "me".
    expect(matchesView(task({ assignee_id: "u1" }), mine)).toBe(false);

    const free: BoardView = { id: "f", label: "f", assignee: "unassigned" };
    expect(matchesView(task({}), free)).toBe(true);
    expect(matchesView(task({ assignee_id: "u1" }), free)).toBe(false);
  });
});

describe("matchesQuery", () => {
  it("matches title, key and labels case-insensitively", () => {
    const t = task({ title: "Fix login", key: "FORGE-9", labels: ["auth"] });
    expect(matchesQuery(t, "LOGIN")).toBe(true);
    expect(matchesQuery(t, "forge-9")).toBe(true);
    expect(matchesQuery(t, "auth")).toBe(true);
    expect(matchesQuery(t, "database")).toBe(false);
  });

  it("treats an empty query as match-all", () => {
    expect(matchesQuery(task({ title: "x" }), "   ")).toBe(true);
  });
});

describe("filterTasks", () => {
  const tasks = [
    task({ id: "1", title: "Login", status: "blocked", priority: "high" }),
    task({ id: "2", title: "Logout", status: "in_progress", priority: "low" }),
  ];

  it("applies view and query together", () => {
    const result = filterTasks(tasks, {
      view: { id: "v", label: "v", statuses: ["blocked"] },
      query: "log",
    });
    expect(result.map((t) => t.id)).toEqual(["1"]);
  });

  it("has a High priority preset that keeps only high/urgent", () => {
    const hot = PRESET_VIEWS.find((v) => v.id === "hot")!;
    const result = filterTasks(tasks, { view: hot, query: "" });
    expect(result.map((t) => t.id)).toEqual(["1"]);
  });
});

describe("saved views persistence", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("round-trips saved views through localStorage", () => {
    const views: BoardView[] = [{ id: "s1", label: "Mine hot", priorities: ["urgent"] }];
    persistSavedViews(views);
    expect(loadSavedViews()).toEqual(views);
  });

  it("returns an empty list for absent or corrupt storage", () => {
    expect(loadSavedViews()).toEqual([]);
    window.localStorage.setItem(SAVED_VIEWS_KEY, "not json");
    expect(loadSavedViews()).toEqual([]);
  });
});
