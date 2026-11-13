# forge-world

The world model: an append-only, bitemporal fact ledger answering *what is
true*, *since when*, *according to whom*, and *what did we believe when we
decided X*.

- **Two clocks.** Every fact carries a world interval (`valid_from`/`valid_to`
  with per-bound precision) and a record interval (`created_at`/
  `invalidated_at`). `holds_at`/`held_at` exist exactly once in Python
  (`forge_world.time`) and once as SQL (the repository).
- **Append-only with a one-way tombstone.** Facts are never updated except the
  single `invalidated_at` stamp; a correction is invalidate + insert. A
  partial unique index makes duplicate live facts impossible.
- **Machines propose, people decide.** Agents create proposals carrying a
  shadow governance verdict; only a person who is not the proposer can accept.
- **Server-verified extraction.** A model proposes statements with verbatim
  quotes; the server checks quote, names and time words, computes offsets, and
  stores every rejection as a coded drop row.
- **Reasoning and drift.** Symmetric/inverse/sub-property/transitive
  consequences are derived with proofs (asserted always beats derived), and a
  recorded decision can be checked for drift against the facts it rested on.

Persistence is `forge_db` (Postgres; SQLite on the unit lane); the HTTP surface
is the `/world` router in `forge-api`. This package never imports the agent
runtime or the API app.
