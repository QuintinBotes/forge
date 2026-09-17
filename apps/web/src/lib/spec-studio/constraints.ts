import type { SpecConstraint } from "@/lib/api/types";

/**
 * Helpers for a constraint that may arrive in either shape.
 *
 * Constraints gained an `{id, text}` form so they can be cited — an ADR
 * rejecting an option "because it violates C2" needs something to point at —
 * but every manifest written before that is a list of bare strings, and those
 * are still on disk and in version history. Both shapes reach the UI, so
 * rendering and editing go through these rather than assuming one.
 */
export type ConstraintLike = string | SpecConstraint;

/** The human-readable text, whichever shape it arrived in. */
export function constraintText(constraint: ConstraintLike): string {
  return typeof constraint === "string" ? constraint : constraint.text;
}

/**
 * A stable React key.
 *
 * Falls back to the index for a bare string, because its text is the only thing
 * identifying it and duplicate text would otherwise collide.
 */
export function constraintKey(constraint: ConstraintLike, index: number): string {
  return typeof constraint === "string" ? `c-${index}` : constraint.id;
}

/** Rewrite a constraint's text, preserving its id when it has one. */
export function withConstraintText(
  constraint: ConstraintLike,
  text: string,
): ConstraintLike {
  return typeof constraint === "string" ? text : { ...constraint, text };
}
