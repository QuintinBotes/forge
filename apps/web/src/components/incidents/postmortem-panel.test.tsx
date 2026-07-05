import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PostmortemView } from "@/lib/api/types";

import { PostmortemPanel } from "./postmortem-panel";

function postmortem(overrides: Partial<PostmortemView> = {}): PostmortemView {
  return {
    id: "pm1",
    incident_id: "i1",
    status: "draft",
    root_cause: "A bad config push disabled connection pooling.",
    content_md:
      "# Summary\nCheckout latency spiked.\n\n## Timeline\n- Alert fired\n- **Rolled back** the deploy",
    action_item_task_keys: ["ENG-101", "ENG-102"],
    ...overrides,
  };
}

describe("PostmortemPanel", () => {
  it("renders the root cause, rendered markdown and action items", () => {
    render(
      <PostmortemPanel postmortem={postmortem()} isLoading={false} isError={false} />,
    );
    expect(screen.getByTestId("postmortem")).toBeInTheDocument();
    expect(screen.getByText(/bad config push/i)).toBeInTheDocument();
    // Heading rendered from markdown (not raw "# Summary").
    expect(screen.getByText("Summary")).toBeInTheDocument();
    // Inline bold rendered as <strong>.
    expect(screen.getByText("Rolled back").tagName).toBe("STRONG");
    // Action items are chips.
    expect(screen.getAllByTestId("action-item")).toHaveLength(2);
    expect(screen.getByText("ENG-101")).toBeInTheDocument();
  });

  it("shows an empty action-items note when none are filed", () => {
    render(
      <PostmortemPanel
        postmortem={postmortem({ action_item_task_keys: [] })}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByTestId("action-items-empty")).toBeInTheDocument();
  });

  it("shows the empty state when there is no postmortem", () => {
    render(<PostmortemPanel postmortem={null} isLoading={false} isError={false} />);
    expect(screen.getByTestId("postmortem-empty")).toBeInTheDocument();
    expect(screen.getByText(/no postmortem yet/i)).toBeInTheDocument();
  });

  it("renders a loading skeleton", () => {
    render(<PostmortemPanel postmortem={undefined} isLoading isError={false} />);
    expect(screen.getByTestId("postmortem-skeleton")).toBeInTheDocument();
  });

  it("renders an error state", () => {
    render(<PostmortemPanel postmortem={undefined} isLoading={false} isError />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn't load the postmortem/i);
  });

  it("publishes when allowed", () => {
    const onPublish = vi.fn();
    render(
      <PostmortemPanel
        postmortem={postmortem()}
        isLoading={false}
        isError={false}
        canPublish
        onPublish={onPublish}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /publish postmortem/i }));
    expect(onPublish).toHaveBeenCalledTimes(1);
  });
});
