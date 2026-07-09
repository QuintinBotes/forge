import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "./client";
import {
  specKeys,
  useApproveSpec,
  useClarifySpec,
  useCreateSpec,
  useGenerateTasks,
  usePlanSpec,
  useSpecOverview,
  useValidateSpec,
} from "./spec";
import type { SpecDashboard, SpecManifest, TaskDTO, ValidationReport } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

const dashboard: SpecDashboard = {
  project_id: "p1",
  constitution: { principles: ["Ship small"], architecture_guardrails: [] },
  specs: [
    { id: "s1", name: "Passwordless auth", status: "clarifying" },
    { id: "s2", name: "Billing v2", status: "validated" },
  ],
};

describe("useSpecOverview", () => {
  it("fetches the project's spec dashboard via the client", async () => {
    const client = {
      getProjectSpecOverview: vi.fn(() => Promise.resolve(dashboard)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    const { result } = renderHook(() => useSpecOverview("p1", client), {
      wrapper: makeWrapper(queryClient),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(dashboard);
    expect(client.getProjectSpecOverview).toHaveBeenCalledWith("p1");
  });
});

describe("useApproveSpec (optimistic)", () => {
  it("flips the spec's status to approved before the request resolves", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(specKeys.overview("p1"), dashboard);

    let resolve!: (value: SpecManifest) => void;
    const pending = new Promise<SpecManifest>((r) => {
      resolve = r;
    });
    const client = {
      approveSpec: vi.fn(() => pending),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useApproveSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => {
      const data = queryClient.getQueryData<SpecDashboard>(specKeys.overview("p1"));
      expect(data?.specs.find((s) => s.id === "s1")?.status).toBe("approved");
    });

    act(() => {
      resolve({ id: "s1", name: "Passwordless auth", status: "approved" });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("rolls the dashboard back when approval fails", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(specKeys.overview("p1"), dashboard);

    const client = {
      approveSpec: vi.fn(() => Promise.reject(new Error("gate error"))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useApproveSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const data = queryClient.getQueryData<SpecDashboard>(specKeys.overview("p1"));
    expect(data?.specs.find((s) => s.id === "s1")?.status).toBe("clarifying");
  });
});

describe("useClarifySpec (optimistic, Describe step)", () => {
  it("flips the spec's status to clarifying before the request resolves", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(specKeys.overview("p1"), {
      ...dashboard,
      specs: [{ id: "s1", name: "Passwordless auth", status: "draft" as const }],
    });

    let resolve!: (value: SpecManifest) => void;
    const pending = new Promise<SpecManifest>((r) => {
      resolve = r;
    });
    const client = {
      clarifySpec: vi.fn(() => pending),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useClarifySpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => {
      const data = queryClient.getQueryData<SpecDashboard>(specKeys.overview("p1"));
      expect(data?.specs.find((s) => s.id === "s1")?.status).toBe("clarifying");
    });

    act(() => {
      resolve({ id: "s1", name: "Passwordless auth", status: "clarifying" });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.clarifySpec).toHaveBeenCalledWith("s1");
  });

  it("rolls the dashboard back when clarification fails", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(specKeys.overview("p1"), dashboard);
    const client = {
      clarifySpec: vi.fn(() => Promise.reject(new Error("boom"))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useClarifySpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const data = queryClient.getQueryData<SpecDashboard>(specKeys.overview("p1"));
    expect(data?.specs.find((s) => s.id === "s1")?.status).toBe("clarifying");
  });
});

describe("usePlanSpec (Refine step, not optimistic)", () => {
  it("calls planSpec and invalidates the spec caches on settle", async () => {
    const planned: SpecManifest = {
      id: "s1",
      name: "Passwordless auth",
      status: "clarifying",
      plan_ref: "plan.md",
    };
    const client = {
      planSpec: vi.fn(() => Promise.resolve(planned)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => usePlanSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.planSpec).toHaveBeenCalledWith("s1");
    expect(result.current.data).toEqual(planned);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: specKeys.all() });
  });
});

describe("useGenerateTasks (Build step, not optimistic)", () => {
  it("calls generateTasks and returns the task list", async () => {
    const tasks: TaskDTO[] = [{ id: "t1", title: "Implement R1" }];
    const client = {
      generateTasks: vi.fn(() => Promise.resolve(tasks)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result } = renderHook(() => useGenerateTasks(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.generateTasks).toHaveBeenCalledWith("s1");
    expect(result.current.data).toEqual(tasks);
  });
});

describe("useValidateSpec (Verify step, not optimistic)", () => {
  it("regenerates tasks to resolve a task id, then validates it", async () => {
    const tasks: TaskDTO[] = [{ id: "t1", title: "Implement R1" }];
    const report: ValidationReport = { task_id: "t1", passed: true };
    const client = {
      generateTasks: vi.fn(() => Promise.resolve(tasks)),
      validateTask: vi.fn(() => Promise.resolve(report)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result } = renderHook(() => useValidateSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.generateTasks).toHaveBeenCalledWith("s1");
    expect(client.validateTask).toHaveBeenCalledWith("t1");
    expect(result.current.data).toEqual(report);
  });

  it("fails with a clear message when there are no tasks to validate", async () => {
    const client = {
      generateTasks: vi.fn(() => Promise.resolve([])),
      validateTask: vi.fn(),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result } = renderHook(() => useValidateSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ specId: "s1" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(client.validateTask).not.toHaveBeenCalled();
  });
});

describe("useCreateSpec", () => {
  it("creates a spec for an epic and invalidates the overview cache", async () => {
    const created: SpecManifest = { id: "s3", name: "New spec", status: "draft" };
    const client = {
      createSpec: vi.fn(() => Promise.resolve(created)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useCreateSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ epic_id: "e1", name: "New spec" });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.createSpec).toHaveBeenCalledWith({ epic_id: "e1", name: "New spec" });
    expect(result.current.data).toEqual(created);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: specKeys.overviews() });
  });

  it("follows up with a PUT to persist Guided-mode fields the create endpoint can't take", async () => {
    const created: SpecManifest = { id: "s3", name: "New spec", status: "draft" };
    const saved: SpecManifest = {
      ...created,
      acceptance_criteria: [{ id: "AC1", text: "Given a, When b, Then c" }],
    };
    const client = {
      createSpec: vi.fn(() => Promise.resolve(created)),
      putSpecManifest: vi.fn(() => Promise.resolve(saved)),
    } as unknown as ForgeApiClient;
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });

    const { result } = renderHook(() => useCreateSpec(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({
        epic_id: "e1",
        name: "New spec",
        acceptance_criteria: saved.acceptance_criteria,
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.createSpec).toHaveBeenCalledWith({ epic_id: "e1", name: "New spec" });
    expect(client.putSpecManifest).toHaveBeenCalledWith(
      "s3",
      expect.objectContaining({ acceptance_criteria: saved.acceptance_criteria }),
    );
    expect(result.current.data).toEqual(saved);
  });
});
