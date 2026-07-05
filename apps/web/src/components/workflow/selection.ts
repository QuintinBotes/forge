/** What the editor currently has focused on the canvas: a state or a transition. */
export type Selection =
  | { kind: "node"; id: string }
  | { kind: "edge"; id: string };
