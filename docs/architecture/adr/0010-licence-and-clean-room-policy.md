# 0010. Licence and clean-room policy for absorbed systems

**Status:** Accepted
**Date:** 2026-11-07

## Context

Forge, lore and utopia are Apache-2.0; tervin is MIT. lore's external `floor` engine and its planning-editor packages
have no licence. Utopia's ontology packs keep their own licences (schema.org CC BY-SA 3.0, FOAF CC BY
1.0, IOF MIT). Lore's web UI pulls MPL-2.0 and LGPL-3.0 dependencies. Apache-2.0 gives no rights to
the "Lore", "Utopia" or "DeepLethe" names.

## Decision

- Re-implement from design documents and observed behaviour. Copying code, prompt text or
  documentation from lore or utopia requires the Apache-2.0 §4 notices in a `THIRD_PARTY_NOTICES.md`
  and a header on the file; the default is to write new text.
- Never use, run or vendor unlicensed sources (lore's `floor` engine and planning-editor packages).
- Ontology packs are optional installs under their own licences, never bundled by default; forge
  authors its own software-delivery ontology.
- Nothing from forge is copied into tervin (Apache-2.0 → MIT); tervin integration is protocol-only.
  Anything ported from tervin keeps the MIT notice.
- No "Lore" or "Utopia" branding in forge's product surface.

## Consequences

- Prompts and thresholds must be re-derived and re-measured.
- Reviewers check new files for copied text against these sources.
