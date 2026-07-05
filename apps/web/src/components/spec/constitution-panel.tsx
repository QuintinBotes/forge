import { Landmark, ShieldCheck } from "lucide-react";

import type { Constitution } from "@/lib/api/types";

export interface ConstitutionPanelProps {
  constitution: Constitution | null | undefined;
}

const ROMAN: [number, string][] = [
  [10, "X"],
  [9, "IX"],
  [5, "V"],
  [4, "IV"],
  [1, "I"],
];

function toRoman(value: number): string {
  let n = value;
  let out = "";
  for (const [num, sym] of ROMAN) {
    while (n >= num) {
      out += sym;
      n -= num;
    }
  }
  return out || "I";
}

/**
 * The project constitution — the engineering principles and architecture
 * guardrails every spec is gated against. Principles read as numbered articles
 * because the order is meaningful: earlier articles outrank later ones.
 */
export function ConstitutionPanel({ constitution }: ConstitutionPanelProps) {
  const principles = constitution?.principles ?? [];
  const guardrails = constitution?.architecture_guardrails ?? [];
  const isEmpty =
    !constitution ||
    (principles.length === 0 &&
      guardrails.length === 0 &&
      !constitution.content);

  if (isEmpty) {
    return (
      <div
        data-testid="constitution-empty"
        className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/40 p-10 text-center"
      >
        <Landmark className="h-7 w-7 text-muted-foreground" aria-hidden />
        <p className="text-sm font-medium text-foreground">No constitution set</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Define the project&apos;s engineering principles and architecture
          guardrails to gate every spec against a shared standard.
        </p>
      </div>
    );
  }

  return (
    <div data-testid="constitution-panel" className="flex flex-col gap-6">
      {principles.length > 0 ? (
        <section className="flex flex-col gap-3">
          <h3 className="flex items-center gap-2 font-display text-sm font-semibold tracking-tight text-foreground">
            <Landmark className="h-4 w-4 text-primary" aria-hidden />
            Principles
          </h3>
          <ol className="flex flex-col gap-2">
            {principles.map((principle, index) => (
              <li
                key={principle}
                className="flex gap-3 rounded-lg border border-border bg-card/60 px-4 py-3"
              >
                <span className="mt-0.5 shrink-0 font-mono text-xs font-semibold text-primary">
                  {toRoman(index + 1)}
                </span>
                <span className="text-sm text-foreground">{principle}</span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {guardrails.length > 0 ? (
        <section className="flex flex-col gap-3">
          <h3 className="flex items-center gap-2 font-display text-sm font-semibold tracking-tight text-foreground">
            <ShieldCheck className="h-4 w-4 text-primary" aria-hidden />
            Architecture guardrails
          </h3>
          <ul className="flex flex-col gap-1.5">
            {guardrails.map((rail) => (
              <li
                key={rail}
                className="flex items-start gap-2 text-sm text-muted-foreground"
              >
                <span
                  aria-hidden
                  className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-spark"
                />
                {rail}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {constitution?.content ? (
        <section className="flex flex-col gap-2">
          <h3 className="font-display text-sm font-semibold tracking-tight text-foreground">
            Notes
          </h3>
          <p className="whitespace-pre-line text-sm leading-relaxed text-muted-foreground">
            {constitution.content}
          </p>
        </section>
      ) : null}
    </div>
  );
}
