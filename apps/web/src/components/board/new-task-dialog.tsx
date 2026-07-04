"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Priority, TaskDTO } from "@/lib/api/types";
import { PRIORITIES, PRIORITY_LABELS } from "@/lib/board/status";

export interface NewTaskInput {
  title: string;
  priority: Priority;
}

export interface NewTaskDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreate: (input: NewTaskInput) => void;
  pending?: boolean;
}

/** Minimal quick-create form opened from the command palette / toolbar. */
export function NewTaskDialog({
  open,
  onOpenChange,
  onCreate,
  pending = false,
}: NewTaskDialogProps) {
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState<Priority>("medium");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = title.trim();
    if (!trimmed) {
      return;
    }
    onCreate({ title: trimmed, priority });
    setTitle("");
    setPriority("medium");
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create task</DialogTitle>
          <DialogDescription>Add a new task to the board.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Title</span>
            <input
              autoFocus
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="What needs to be done?"
              className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Priority</span>
            <select
              value={priority}
              onChange={(event) => setPriority(event.target.value as Priority)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {PRIORITIES.map((value) => (
                <option key={value} value={value}>
                  {PRIORITY_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={pending || !title.trim()}>
              {pending ? "Creating…" : "Create task"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Build the task DTO sent to the API from the quick-create form input. */
export function toCreatePayload(input: NewTaskInput): TaskDTO {
  return {
    title: input.title,
    priority: input.priority,
    kind: "feature",
    status: "backlog",
  };
}
