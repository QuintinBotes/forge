"""Command-output capping + artifact offload (F19 AC10).

``stdout``/``stderr`` are capped at ``FORGE_SANDBOX_OUTPUT_CAP_BYTES`` before being
returned in a :class:`~forge_contracts.CommandOutput`. When the full output
exceeds the cap and an :class:`ArtifactStore` is available, the complete bytes are
stored (e.g. MinIO) and referenced via ``*_artifact_ref``; the inline field keeps
only the truncated prefix.
"""

from __future__ import annotations

from forge_agent.sandbox.base import ArtifactStore


def cap_output(
    text: str,
    *,
    cap_bytes: int,
    store: ArtifactStore | None,
    key: str,
) -> tuple[str, str | None]:
    """Return ``(inline_text, artifact_ref)`` honoring the byte cap.

    If ``text`` is within the cap, it is returned unchanged with no ref. If it
    exceeds the cap it is truncated to ``cap_bytes`` and — when ``store`` is set —
    the full bytes are offloaded and the returned ref points at them.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap_bytes:
        return text, None
    truncated = encoded[:cap_bytes].decode("utf-8", errors="ignore")
    ref: str | None = None
    if store is not None:
        ref = store.put(key, encoded, content_type="text/plain")
    return truncated, ref


__all__ = ["cap_output"]
