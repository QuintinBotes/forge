import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ForgeApiClient } from "./client";
import { queryKeys, useSetTaskStatus } from "./hooks";
import type { TaskDTO } from "./types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

const baseTask: TaskDTO = {
  id: "t1",
  title: "Build login",
  status: "backlog",
};

describe("useSetTaskStatus (optimistic)", () => {
  it("applies the new status to the cached list immediately (before the request resolves)", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(queryKeys.tasks(), [baseTask]);

    // A request we control: stays pending until we resolve it.
    let resolve!: (value: TaskDTO) => void;
    const pending = new Promise<TaskDTO>((r) => {
      resolve = r;
    });
    const client = {
      setTaskStatus: vi.fn(() => pending),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useSetTaskStatus(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ taskId: "t1", status: "in_progress" });
    });

    // Optimistic: cache reflects the new status while the request is still pending.
    await waitFor(() => {
      const tasks = queryClient.getQueryData<TaskDTO[]>(queryKeys.tasks());
      expect(tasks?.[0].status).toBe("in_progress");
    });

    act(() => {
      resolve({ ...baseTask, status: "in_progress" });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("rolls back the optimistic change when the request fails", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    queryClient.setQueryData(queryKeys.tasks(), [baseTask]);

    const client = {
      setTaskStatus: vi.fn(() => Promise.reject(new Error("boom"))),
    } as unknown as ForgeApiClient;

    const { result } = renderHook(() => useSetTaskStatus(client), {
      wrapper: makeWrapper(queryClient),
    });

    act(() => {
      result.current.mutate({ taskId: "t1", status: "done" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));

    const tasks = queryClient.getQueryData<TaskDTO[]>(queryKeys.tasks());
    expect(tasks?.[0].status).toBe("backlog");
  });
});
