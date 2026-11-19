"""Attested Changesets: the DSSE envelope + in-toto Statement contract.

This is an **additive** extension of the frozen ``scf_contracts`` surface
(the same pattern :mod:`scf_contracts.orchestration_config` uses): the DTOs
below live in their own module namespace and do **not** mutate the frozen
top-level ``__all__``.

Three layers, outside in:

- :class:`DsseEnvelope` — a `Dead Simple Signing Envelope
  <https://github.com/secure-systems-lab/dsse>`_: ``payloadType`` + base64
  ``payload`` + detached ``signatures[]``. The envelope never carries the
  payload bytes in the clear-signed sense — signing/verifying goes over the
  PAE (Pre-Authentication Encoding) of ``(payloadType, payload)``, which is
  the signer/verifier's job (crypto lives in ``scf_marketplace.verifier``'s
  Ed25519 pattern), not this contract module's.
- :class:`Statement` — an `in-toto v1 Statement
  <https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md>`_:
  ``_type`` + ``subject[]`` (what artifact this is about) + ``predicateType``
  + ``predicate`` (an opaque, predicate-type-specific object). A Statement is
  generic — any predicate type can ride in it — so ``predicate`` stays a
  plain JSON-compatible ``dict``; use :func:`encode_statement` /
  :func:`decode_statement` to move one into and out of a
  :class:`DsseEnvelope`'s base64 ``payload``.
- :class:`ChangesetProvenance` — the one SCF predicate this slice defines
  (``predicateType`` = :data:`CHANGESET_PROVENANCE_PREDICATE_TYPE`): the
  truthful runtime facts about the agent run that produced a changeset.
  Every field attests what actually ran, never a planned/intended value
  (e.g. ``sandbox_tier`` comes off the ``SandboxSpec`` the run actually
  executed under; there is deliberately no "planned tier" field since
  Adaptive Orchestration's ``plan_execution`` has no production caller yet).

The tamper-evident audit chain (F39, ``scf_db.audit``) is the other half of
this seam: ``AuditEvent.detail_ref`` (``{"table", "id"}``) is the hook a later
slice uses to chain a persisted attestation row into the hash chain, and
:func:`scf_contracts.audit.canonical_json` is reused here (rather than
re-implemented) so both the audit chain and an attestation payload hash the
exact same way — sorted keys, no whitespace, ``str()`` fallback for extras.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from scf_contracts.audit import canonical_json
from scf_contracts.sandbox import SandboxKind

__all__ = [
    "CHANGESET_PROVENANCE_PREDICATE_TYPE",
    "DSSE_PAYLOAD_TYPE_INTOTO",
    "INTOTO_STATEMENT_TYPE",
    "SCF_AGENT",
    "AssistanceIdentity",
    "ChangesetProvenance",
    "CheckOutcome",
    "DigestSet",
    "DsseEnvelope",
    "DsseSignature",
    "HeldOutSummary",
    "StageVerdict",
    "Statement",
    "Subject",
    "Verification",
    "decode_statement",
    "encode_statement",
]

#: DSSE ``payloadType`` for a payload that is an in-toto Statement (DSSE spec's
#: worked example / the in-toto Attestation Spec's own convention).
DSSE_PAYLOAD_TYPE_INTOTO = "application/vnd.in-toto+json"

#: The in-toto Statement ``_type`` value (in-toto Attestation Spec v1). The
#: spec version is baked into the URL itself, so this is a single constant,
#: not an open vocabulary.
INTOTO_STATEMENT_TYPE: Literal["https://in-toto.io/Statement/v1"] = (
    "https://in-toto.io/Statement/v1"
)

#: SCF's ``predicateType`` for a :class:`ChangesetProvenance` predicate.
CHANGESET_PROVENANCE_PREDICATE_TYPE = (
    "https://github.com/QuintinBotes/forge/attestations/changeset-provenance/v1"
)


class _Model(BaseModel):
    """Shared base: tolerant of unknown keys, populatable by field name or alias.

    ``frozen=True`` — an attestation, once built, is an immutable statement of
    fact; nothing downstream (signer, verifier, chain writer) mutates it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class DsseSignature(_Model):
    """One signature over a DSSE envelope's PAE-encoded payload."""

    keyid: str
    sig: str  # base64-encoded raw signature bytes


class DsseEnvelope(_Model):
    """A Dead Simple Signing Envelope (payloadType + base64 payload + signatures).

    ``payload`` is always the base64 encoding of the raw payload bytes (an
    in-toto Statement's canonical JSON, here) — never the payload itself.
    """

    payloadType: str
    payload: str  # base64
    signatures: list[DsseSignature] = Field(default_factory=list)


class DigestSet(_Model):
    """in-toto ``DigestSet``: one or more ``{algorithm: hex_digest}`` pairs.

    ``sha256`` is what SCF populates for a workflow-run subject; a commit subject
    carries only ``gitCommit`` (#175). ``extra="allow"`` (instead of the base's
    ``"ignore"``) lets a producer attach additional algorithms without them being
    silently dropped on validation, and an absent algorithm is left out when
    serialized, so a ``sha256``-only set serializes as it always did.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    sha256: str | None = None

    @model_validator(mode="after")
    def _at_least_one_digest(self) -> DigestSet:
        if self.sha256 is None and not self.model_extra:
            raise ValueError("a DigestSet needs at least one digest")
        return self

    @model_serializer(mode="wrap")
    def _without_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {k: v for k, v in handler(self).items() if v is not None}


class Subject(_Model):
    """One in-toto ``subject`` entry: the artifact this Statement is about."""

    name: str
    digest: DigestSet


class Statement(_Model):
    """An in-toto v1 Statement: ``_type`` + ``subject[]`` + predicate envelope."""

    type_: Literal["https://in-toto.io/Statement/v1"] = Field(
        alias="_type", default=INTOTO_STATEMENT_TYPE
    )
    subject: list[Subject]
    predicateType: str
    predicate: dict[str, Any] = Field(default_factory=dict)


class CheckOutcome(_Model):
    """One service-side check SCF ran on the head commit (#172)."""

    id: str
    outcome: Literal["passed", "failed"]


class StageVerdict(_Model):
    """A verification stage's verdict and the model that gave it.

    ``not_run`` when the stage did not run: never omitted, never a pass (#175).
    """

    verdict: str
    model: str | None = None


class HeldOutSummary(_Model):
    """The held-out scenarios run on the head commit, as counts only (#286).

    ``verdict`` is ``passed`` or ``failed`` over the approved scenarios that ran;
    ``none_defined`` or ``none_approved`` when none ran, ``could_not_verify`` when the
    change touched a file that hooks into the test run, and ``not_run`` when the
    stage was not reached: none of those is a pass. ``defined`` counts the run's
    scenarios that are not withdrawn, whatever their state; only approved ones run.
    """

    verdict: str = "not_run"
    defined: int = 0
    passed: int = 0
    failed: int = 0


class Verification(_Model):
    """What SCF verified about the change before a person reviewed it (#175)."""

    #: ``passed``, ``failed`` or ``not_run`` over all of ``checks``.
    checks_verdict: str
    checks: list[CheckOutcome] = Field(default_factory=list)
    #: The CI status of the head commit, ``not_run`` while it is not read back (#280).
    ci: str = "not_run"
    reviewer: StageVerdict
    red_team: StageVerdict
    held_out: HeldOutSummary = Field(default_factory=HeldOutSummary)


#: The agent name SCF writes in each commit's ``Assisted-by:`` trailer (#214).
SCF_AGENT = "scf-agent"


class AssistanceIdentity(_Model):
    """Who assisted a change: SCF's agent and the model the run used (#214).

    ``model`` is ``None`` when the run's model is unknown; it is never guessed.
    """

    agent: str = SCF_AGENT
    model: str | None = Field(default=None, pattern=r"^\S+$")

    @property
    def trailer(self) -> str:
        """The ``Assisted-by:`` value: ``<agent>:<model>``, or the agent alone."""
        return f"{self.agent}:{self.model}" if self.model else self.agent


class ChangesetProvenance(_Model):
    """SCF's in-toto predicate for one attested changeset.

    Field provenance (attest the TRUTHFUL runtime value, never a planned one):

    - ``agent_role``/``model`` — :attr:`AgentRun.role` / :attr:`AgentRun.model`
      (what actually ran).
    - ``model_version`` — the per-model version breakdown from
      ``AgentRunResult.artifacts["model_usage"]``, when the provider reports one.
    - ``prompt_spec_revision`` — the :attr:`SpecVersion.version_number` the
      agent's prompt was built against.
    - ``sandbox_tier`` — :attr:`SandboxSpec.kind`, the isolation the run's
      commands actually executed under.
    - ``policy_version_hash`` — content hash of the policy version in effect
      for the run.
    - ``tool_calls`` — tool names invoked, in order, distilled from
      ``AgentRun.steps``. Names only (no arguments): an attestation may be
      exported/published outside the trust boundary the raw step log lives in.
    - ``human_approver`` — the approving user id when a human approved the
      change (``None`` when auto-approved or not yet approved).
    - ``workflow_run_id``/``agent_run_id`` — the run this changeset came from.
    - ``pr_numbers`` — :attr:`TraceabilityCriterionLink.pr_numbers` (empty
      until a PR exists).
    - ``spec_key``/``spec_version`` — the spec (and version) this changeset is
      attested against, which may have advanced past ``prompt_spec_revision``
      by the time the changeset is attested.
    - ``repository``/``head_commit`` — the change's repository and head commit
      when it is attested for its pull request (#175).
    - ``verification`` — the checks, CI, reviewer and red-team verdicts (#175).
      Absent on attestations made before #175.
    - ``base_commit``/``head_commit``/``assisted_by`` — the commit the change starts
      from, its head and who assisted it, when the run made commits (#214). Absent on
      attestations made before #214.
    """

    agent_role: str
    model: str
    model_version: str | None = None
    prompt_spec_revision: int
    sandbox_tier: SandboxKind
    policy_version_hash: str
    tool_calls: list[str] = Field(default_factory=list)
    human_approver: UUID | None = None
    workflow_run_id: UUID
    agent_run_id: UUID
    pr_numbers: list[int] = Field(default_factory=list)
    spec_key: str
    spec_version: int
    repository: str | None = None
    head_commit: str | None = None
    verification: Verification | None = None
    base_commit: str | None = None
    assisted_by: list[AssistanceIdentity] = Field(default_factory=list)


def encode_statement(statement: Statement) -> str:
    """Canonically serialize ``statement`` into a DSSE-envelope-ready base64 payload.

    Uses :func:`scf_contracts.audit.canonical_json` (sorted keys, no
    whitespace) so re-encoding the same Statement always yields byte-identical
    output — required for a signature over the PAE-encoded payload to verify
    consistently across producers/verifiers. The result is exactly what goes
    into :attr:`DsseEnvelope.payload`.
    """
    canonical = canonical_json(statement.model_dump(mode="json", by_alias=True))
    return base64.b64encode(canonical.encode("utf-8")).decode("ascii")


def decode_statement(payload_b64: str) -> Statement:
    """Inverse of :func:`encode_statement`: recover the :class:`Statement`.

    Takes a base64 ``payload`` (e.g. :attr:`DsseEnvelope.payload`) and returns
    the validated :class:`Statement` it encodes.
    """
    raw = base64.b64decode(payload_b64.encode("ascii"))
    return Statement.model_validate(json.loads(raw))
