"""Temporal client construction + the Forge data converter (F25).

The data converter is the pydantic payload converter (so ``UUID`` / ``WorkflowState``
StrEnum / ``datetime`` round-trip) wrapped by the :class:`RedactingEncryptionCodec`
so secrets never enter Temporal's durable history. The same converter is
registered on the client *and* the worker.
"""

from __future__ import annotations

import dataclasses

from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter

from forge_workflow.temporal.config import TemporalSettings
from forge_workflow.temporal.converter import RedactingEncryptionCodec


def build_data_converter(codec_key: str | None) -> DataConverter:
    """Pydantic converter + (optional) redacting/encrypting codec.

    When ``codec_key`` is set the codec is registered (production / temporal
    backend). When unset (unit tests, plaintext dev) the bare pydantic converter
    is used — payloads still round-trip; encryption-at-rest is simply off.
    """
    if not codec_key:
        return pydantic_data_converter
    return dataclasses.replace(
        pydantic_data_converter, payload_codec=RedactingEncryptionCodec(codec_key)
    )


def _tls_config(settings: TemporalSettings) -> TLSConfig | None:
    if not (settings.temporal_tls_cert and settings.temporal_tls_key):
        return None
    return TLSConfig(
        client_cert=settings.temporal_tls_cert.encode(),
        client_private_key=settings.temporal_tls_key.encode(),
        server_root_ca_cert=(
            settings.temporal_tls_ca.encode() if settings.temporal_tls_ca else None
        ),
    )


async def get_temporal_client(settings: TemporalSettings | None = None) -> Client:
    """Connect a Temporal client from ``TemporalSettings`` (env-driven)."""
    settings = settings or TemporalSettings()
    return await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        data_converter=build_data_converter(settings.temporal_codec_key),
        tls=_tls_config(settings) or False,
    )


__all__ = ["build_data_converter", "get_temporal_client"]
